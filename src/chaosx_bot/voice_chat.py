"""Real-time voice chat for the ChaosX bot: public live channel with voice answers.

Protocol layer (RTP de-framing, NaCl transport decrypt, DAVE E2EE decrypt, per-speaker Opus
decode, silence segmentation) adapted from the proven Discord voice-receive implementation in
Hermes Agent — MIT License, Copyright (c) 2025 Nous Research.

Public-safety design:
  * answers come from a caller-supplied ``answer_fn`` so voice uses the same sandboxed,
    tool-free public ask path as the text bot — voice never gains new capabilities;
  * every utterance is rate limited per user (cooldown + hourly cap) and the work queue is
    bounded, so a public channel cannot be used to spam or to run up unbounded API cost;
  * spoken answers are trimmed to a length that makes sense read aloud.

Concurrency: each speaker arrives as its own stream (SSRC -> user_id via the SPEAKING opcode),
so buffers are per speaker; a single worker consumes the queue, which serialises answers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import struct
import time
import wave
from collections import defaultdict, deque
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

import discord

logger = logging.getLogger(__name__)


# If a transcript is nothing but these words, the recogniser echoed the prompt instead of
# transcribing speech (how the old "hi" -> glossary-terms hallucination happened).
PROMPT_ECHO_WORDS = frozenset({"chaos", "redux", "super", "event", "events", "hoi4", "mod"})


def looks_like_prompt_echo(text: str) -> bool:
    """True when a transcript is only prompt words (no actual speech behind it)."""
    words = {w.strip(".,!?;:\"'()").lower() for w in text.split()}
    words = {w for w in words if w}
    return bool(words) and words <= PROMPT_ECHO_WORDS


def _log(message: str) -> None:
    """Console line in the bot's own style (its module loggers are filtered by logging setup)."""
    print(f"[voice] {message}", flush=True)

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2
SILENCE_THRESHOLD = 0.5          # seconds of quiet that ends an utterance (0.9s was audibly laggy)
MIN_SPEECH_DURATION = 0.45       # ignore coughs/clicks shorter than this
SPEECH_FLOOR_DBFS = -38.0        # breathing and room noise sit far below this; speech measured -8 to -10
BARGE_IN_MIN_SECONDS = 0.25      # an interruption has to be sustained, not one noisy frame
MAX_UTTERANCE_SECONDS = 30.0     # hard cap so one long monologue can't balloon STT cost


class VoiceReceiver:
    """Per-speaker audio capture from a connected ``discord.VoiceClient``."""

    def __init__(self, vc: discord.VoiceClient, allowed_user_ids: Optional[set[int]] = None):
        self._vc = vc
        conn = vc._connection
        self._conn = conn
        # discord.py exposes the transport key as a list of ints; NaCl wants raw bytes.
        raw_key = conn.secret_key
        self._secret_key: bytes = raw_key if isinstance(raw_key, bytes) else bytes(raw_key)
        self._dave_session = getattr(conn, "dave_session", None)
        self._bot_ssrc: int = conn.ssrc
        self._allowed_user_ids = allowed_user_ids or set()
        self._ssrc_to_user: dict[int, int] = {}
        self._buffers: dict[int, bytearray] = defaultdict(bytearray)
        self._last_packet_time: dict[int, float] = {}
        self._decoders: dict[int, "discord.opus.Decoder"] = {}
        self._lock = __import__("threading").Lock()
        self._running = False
        self._stats: dict[str, int] = {
            "packets": 0, "bad_rtp": 0, "nacl_fail": 0, "dave_fail": 0,
            "decode_fail": 0, "decoded": 0, "no_user": 0, "utterances": 0,
            "dropped_unmapped": 0, "dropped_not_ready": 0, "no_key": 0,
            "dave_no_cryptor": 0, "plaintext_frames": 0,
        }
        self._first_audio_logged = False

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    # --- lifecycle ---

    def start(self) -> None:
        conn = self._conn
        self._install_speaking_hook(conn)
        conn.add_socket_listener(self._on_packet)
        self._running = True
        logger.info("ChaosX voice receiver started (bot_ssrc=%d)", self._bot_ssrc)

    def stop(self) -> None:
        self._running = False
        try:
            self._conn.remove_socket_listener(self._on_packet)
        except Exception:
            pass
        with self._lock:
            self._buffers.clear()
            self._last_packet_time.clear()
            self._decoders.clear()
            self._ssrc_to_user.clear()

    # --- SSRC -> user_id (SPEAKING opcode 5) ---

    def _current_keys(self):
        """Transport key + DAVE session as they are NOW: both are replaced on every voice reconnect."""
        raw_key = getattr(self._conn, "secret_key", None)
        try:
            key = raw_key if isinstance(raw_key, bytes) else bytes(raw_key) if raw_key else b""
        except Exception:
            key = b""
        return key, getattr(self._conn, "dave_session", None)

    @staticmethod
    def _dbfs(pcm: bytes) -> float:
        """Loudness of a PCM buffer in dBFS (silence and undecryptable noise both land at the extremes)."""
        try:
            import audioop

            rms = audioop.rms(pcm, 2)
        except Exception:
            return float("-inf")
        return 20 * math.log10(rms / 32768) if rms else float("-inf")

    def speech_like_incoming(self) -> bool:
        """True when someone is really talking: sustained audio at speech level, not breathing.

        Barge-in used to fire on any buffered frame, so a breath silenced the bot mid-sentence.
        """
        try:
            needed = int(BARGE_IN_MIN_SECONDS * BYTES_PER_SECOND)
            with self._lock:
                buffers = list(self._buffers.values())
        except Exception:
            return False
        for buf in buffers:
            if len(buf) >= needed and self._dbfs(bytes(buf)) >= SPEECH_FLOOR_DBFS:
                return True
        return False

    def map_ssrc(self, ssrc: int, user_id: int) -> None:
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def _install_speaking_hook(self, conn) -> None:
        original_hook = conn.hook
        receiver = self

        async def wrapped_hook(ws, msg):
            if isinstance(msg, dict) and msg.get("op") == 5:
                data = msg.get("d") or {}
                ssrc, user_id = data.get("ssrc"), data.get("user_id")
                if ssrc and user_id:
                    receiver.map_ssrc(int(ssrc), int(user_id))
            if original_hook:
                await original_hook(ws, msg)

        conn.hook = wrapped_hook
        try:
            from discord.utils import MISSING

            if hasattr(conn, "ws") and conn.ws is not MISSING:
                conn.ws._hook = wrapped_hook
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not install speaking hook on live ws: %s", exc)

    # --- packet path (runs on the socket reader thread) ---

    def _on_packet(self, data: bytes) -> None:
        if not self._running or len(data) < 16:
            return
        self._stats["packets"] += 1
        if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
            self._stats["bad_rtp"] += 1
            return  # not RTP voice
        first_byte = data[0]
        _, _, _seq, _ts, ssrc = struct.unpack_from(">BBHII", data, 0)
        if ssrc == getattr(self._conn, "ssrc", self._bot_ssrc):
            return
        cc = first_byte & 0x0F
        has_extension = bool(first_byte & 0x10)
        header_size = 12 + (4 * cc) + (4 if has_extension else 0)
        if len(data) < header_size + 4:
            return
        header = bytes(data[:header_size])
        payload_with_nonce = data[header_size:]
        if len(payload_with_nonce) < 4:
            return

        # NaCl transport decrypt (aead_xchacha20_poly1305_rtpsize): trailing 4 bytes are the nonce.
        # Read the key fresh: it is rotated whenever Discord reconnects the voice client.
        secret_key, dave_session = self._current_keys()
        if not secret_key:
            self._stats["no_key"] = self._stats.get("no_key", 0) + 1
            return
        nonce = bytearray(24)
        nonce[:4] = payload_with_nonce[-4:]
        try:
            import nacl.secret

            box = nacl.secret.Aead(secret_key)
            decrypted = box.decrypt(payload_with_nonce[:-4], header, bytes(nonce))
        except Exception as exc:
            self._stats["nacl_fail"] += 1
            if self._stats["nacl_fail"] == 1:
                _log(f"transport decrypt failing: {type(exc).__name__}: {exc}")
            return

        self._stats.setdefault("first_frame", {})
        if ssrc not in self._stats["first_frame"] and len(self._stats["first_frame"]) < 4:
            self._stats["first_frame"][ssrc] = True
            _log(
                f"first frame from ssrc={ssrc}: {len(decrypted)}B "
                f"head={decrypted[:8].hex()} tail={decrypted[-4:].hex()}"
            )

        # DAVE end-to-end encryption (passthrough when Discord reports it as unencrypted).
        # With E2EE active, an undecrypted frame is CIPHERTEXT, not Opus: decoding it yields noise,
        # so such frames are dropped rather than turned into audio.
        if dave_session:
            session_ready = bool(getattr(dave_session, "ready", False))
            with self._lock:
                user_id = self._ssrc_to_user.get(ssrc, 0)
            # DAVE marks encrypted payloads with 0xFAFA; a markerless payload is a passthrough frame.
            dave_encrypted = len(decrypted) >= 2 and decrypted[0] == 0xFA and decrypted[1] == 0xFA
            if not user_id:
                # No SPEAKING event told us who this is; the Opus decode below decides if it is audio.
                self._stats["dropped_unmapped"] += 1
            elif not dave_encrypted:
                # Unencrypted (passthrough) frame: decode it directly, DAVE must not touch it.
                self._stats["passthrough_frames"] = self._stats.get("passthrough_frames", 0) + 1
            else:
                try:
                    import davey

                    decrypted = dave_session.decrypt(user_id, davey.MediaType.audio, decrypted)
                except Exception as exc:
                    message = str(exc)
                    if self._stats.get("dave_error_logged") != user_id:
                        self._stats["dave_error_logged"] = user_id
                        _log(
                            f"davey.decrypt(user_id={user_id}) on a "
                            f"{'marked' if dave_encrypted else 'unmarked'} {len(decrypted)}B frame said: "
                            f"{type(exc).__name__}: {message[:160]}"
                        )
                    if "Unencrypted" in message:
                        pass
                    elif "NoValidCryptorFound" in message:
                        # No key for this speaker: either the frame really is plaintext, or the session is
                        # missing the member's MLS key. Log enough to tell those apart.
                        self._stats["dave_no_cryptor"] = self._stats.get("dave_no_cryptor", 0) + 1
                        if self._stats["dave_no_cryptor"] == 1:
                            known: object = "?"
                            try:
                                known = sorted(int(uid) for uid in dave_session.get_user_ids())
                            except Exception as exc:
                                known = f"{type(exc).__name__}"
                            _log(
                                "DAVE has no key for user "
                                f"{user_id} (session status={getattr(dave_session, 'status', '?')} "
                                f"ready={session_ready} epoch={getattr(dave_session, 'epoch', '?')} "
                                f"known users={known}); dropping the encrypted frames"
                            )
                        # The 0xFAFA marker proves this payload is ciphertext, so the Opus decoder cannot
                        # read it: drop the frame instead of turning it into noise and error spam.
                        return
                    else:
                        self._stats["dave_fail"] += 1
                        if self._stats["dave_fail"] == 1:
                            _log(f"DAVE decrypt failing: {type(exc).__name__}: {exc}")
                        return
            if not session_ready:
                self._stats["dropped_not_ready"] = self._stats.get("dropped_not_ready", 0) + 1

        try:
            if ssrc not in self._decoders:
                self._decoders[ssrc] = discord.opus.Decoder()
            pcm = self._decoders[ssrc].decode(decrypted)
            if dave_session and self._stats.get("dave_no_cryptor"):
                self._stats["plaintext_frames"] += 1
        except Exception as exc:
            self._decoders.pop(ssrc, None)
            self._stats["decode_fail"] += 1
            if self._stats["decode_fail"] == 1:
                _log(f"opus decode failing: {type(exc).__name__}: {exc}")
            return

        self._stats["decoded"] += 1
        if not self._first_audio_logged:
            self._first_audio_logged = True
            with self._lock:
                who = self._ssrc_to_user.get(ssrc, 0)
            _log(f"hearing audio: first decoded packet from ssrc={ssrc} user={who or 'unknown'}")

        with self._lock:
            buf = self._buffers[ssrc]
            buf.extend(pcm)
            # Cap one utterance so a monologue can't grow STT cost without bound.
            if len(buf) > int(MAX_UTTERANCE_SECONDS * BYTES_PER_SECOND):
                del buf[: len(buf) - int(MAX_UTTERANCE_SECONDS * BYTES_PER_SECOND)]
            self._last_packet_time[ssrc] = time.monotonic()

    # --- utterance assembly ---

    def _infer_user_for_ssrc(self, ssrc: int) -> int:
        """After a rejoin Discord may not resend SPEAKING; infer when exactly one candidate is present."""
        try:
            channel = self._vc.channel
            if not channel:
                return 0
            bot_id = self._vc.user.id if self._vc.user else 0
            candidates = [
                m.id
                for m in channel.members
                if m.id != bot_id and (not self._allowed_user_ids or m.id in self._allowed_user_ids)
            ]
            if len(candidates) == 1:
                self._ssrc_to_user[ssrc] = candidates[0]
                return candidates[0]
        except Exception:
            pass
        return 0

    def check_silence(self) -> list[tuple[int, bytes]]:
        """Completed utterances as ``(user_id, pcm_48k_stereo_s16le)``."""
        now = time.monotonic()
        completed: list[tuple[int, bytes]] = []
        with self._lock:
            ssrc_user_map = dict(self._ssrc_to_user)
            for ssrc in list(self._buffers.keys()):
                last_time = self._last_packet_time.get(ssrc, now)
                silence = now - last_time
                buf = self._buffers[ssrc]
                duration = len(buf) / BYTES_PER_SECOND
                if silence >= SILENCE_THRESHOLD and duration >= MIN_SPEECH_DURATION:
                    loudness = self._dbfs(bytes(buf))
                    if loudness < SPEECH_FLOOR_DBFS:
                        # a breath, a keyboard, a fan: drop it instead of transcribing (and answering) noise
                        self._stats["ignored_noise"] = self._stats.get("ignored_noise", 0) + 1
                        if self._stats["ignored_noise"] == 1:
                            _log(
                                f"ignored non-speech sound ({duration:.1f}s at {loudness:.1f} dBFS, "
                                f"floor {SPEECH_FLOOR_DBFS} dBFS)"
                            )
                        self._buffers[ssrc] = bytearray()
                        self._last_packet_time.pop(ssrc, None)
                        continue
                    user_id = ssrc_user_map.get(ssrc, 0) or self._infer_user_for_ssrc(ssrc)
                    if user_id:
                        self._stats["utterances"] += 1
                        completed.append((user_id, bytes(buf)))
                    else:
                        self._stats["no_user"] += 1
                        _log("captured speech but no SPEAKING event told me who it was")
                    self._buffers[ssrc] = bytearray()
                    self._last_packet_time.pop(ssrc, None)
                elif silence >= SILENCE_THRESHOLD * 2:
                    self._buffers.pop(ssrc, None)
                    self._last_packet_time.pop(ssrc, None)
        return completed


_MARKDOWN_PATTERNS = [
    (r"```.*?```", " ", "s"),          # code fences
    (r"`([^`]*)`", r"\1"),             # inline code
    (r"!?\[([^\]]*)\]\([^)]*\)", r"\1"),   # links/images keep their text
    (r"https?://\S+", " a link "),     # bare URLs are never useful spoken
    (r"^[ \t]{0,3}#{1,6}[ \t]*", "", "m"),  # headings
    (r"^[ \t]*[-*+•][ \t]+", "", "m"),      # bullets
    (r"^[ \t]*\d+[.)][ \t]+", "", "m"),    # numbered lists
    (r"[*_~]{1,3}", ""),                # emphasis markers
    (r"^[ \t]*>+[ \t]*", "", "m"),          # quotes
    (r"\|", " "),                      # table pipes
    (r"\s{2,}", " "),
]


def speech_safe(text: str) -> str:
    """Make a reply safe to read aloud: no markdown, no URLs, no stray symbols."""
    import re

    flags_map = {"s": re.DOTALL, "m": re.MULTILINE}
    out = text
    for pattern, replacement, *names in _MARKDOWN_PATTERNS:
        flags = 0
        for name in names:
            flags |= flags_map.get(name, 0)
        out = re.sub(pattern, replacement, out, flags=flags)
    # Collapse whitespace FIRST: the emoji filter below removes non-printable characters, and
    # newlines are not printable, so filtering first would glue words together.
    out = " ".join(out.split())
    # Drop emoji and pictographs that would otherwise be spelled out or skipped awkwardly.
    out = "".join(ch for ch in out if ch.isprintable() and not (0x1F000 <= ord(ch) <= 0x1FAFF))
    out = " ".join(out.split())
    return out.strip(" -–—:;")


def pcm_to_wav(pcm: bytes, path: Path) -> Path:
    """Write raw 48 kHz stereo PCM to a 16-bit WAV for the STT model."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return path


_PREF_FILLERS = (
    "please", "thanks", "thank you", "chaosx", "chaos x", "can you", "could you", "would you",
    "for me", "a bit", "a little", "ok", "okay", "hey", "just",
)

# Exact phrases only, once the filler is stripped: a substring rule turned "stop the black plague from
# spreading" into "stop" and cut the bot off mid-answer.
_PREF_PHRASES: dict[str, str] = {
    "stop": "stop", "stop talking": "stop", "stop it": "stop", "shut up": "stop", "be quiet": "stop",
    "quiet": "stop", "silence": "stop", "be silent": "stop",
    "speak slower": "slower", "slower": "slower", "too fast": "slower", "slow down": "slower",
    "talk slower": "slower", "speak more slowly": "slower",
    "speak faster": "faster", "faster": "faster", "too slow": "faster", "speed up": "faster",
    "talk faster": "faster",
    "keep it short": "short", "keep it brief": "short", "be brief": "short", "shorter answers": "short",
    "short answers": "short", "less detail": "short", "keep your answers short": "short",
    "more detail": "detailed", "more details": "detailed", "be detailed": "detailed",
    "longer answers": "detailed", "full detail": "detailed", "explain more": "detailed",
    "reset my voice settings": "reset", "reset voice settings": "reset", "default voice settings": "reset",
    "normal voice": "reset", "reset my settings": "reset", "reset voice": "reset", "reset": "reset",
}

_IDEA_RE = re.compile(
    r"^\s*(?:note|log|record|save|file|submit|add)?\s*"
    r"(?:this\s+|an\s+|a\s+)?idea\b\s*[:,\-]?\s*(.*)$",
    re.IGNORECASE,
)

_ADDRESS_RE = re.compile(r"^(?:hey|ok|okay|yo)?\s*chaos\s?x\s*[,:]?\s*", re.IGNORECASE)

_YES_WORDS = ("yes", "yeah", "yep", "yup", "sure", "send it", "submit it", "file it", "confirm", "do it", "correct")
_NO_WORDS = ("no", "nope", "nah", "cancel", "forget it", "never mind", "nevermind", "dont", "don't", "stop")


def _idea_request(transcript: str) -> tuple[bool, str]:
    """(did the member hand over an idea, the idea text). An empty body is still an attempt."""
    spoken = _ADDRESS_RE.sub("", (transcript or "").strip()).strip()
    match = _IDEA_RE.match(spoken)
    if not match:
        return False, ""
    return True, " ".join(match.group(1).split()).strip(" :,-")


def _idea_text(transcript: str) -> str | None:
    """The idea in "note this idea: ..." style utterances, or None when there is nothing to file."""
    matched, body = _idea_request(transcript)
    return (body or None) if matched else None


def _confirmation(transcript: str) -> bool | None:
    """True for a yes, False for a no, None when it is neither (so the utterance is answered normally)."""
    cleaned = " ".join(
        (transcript or "").lower().replace("!", "").replace(".", "").replace(",", "").replace("?", "").split()
    )
    if not cleaned or len(cleaned) > 40:
        return None
    for word in _YES_WORDS:
        if cleaned == word or cleaned.startswith(word + " "):
            return True
    for word in _NO_WORDS:
        if cleaned == word or cleaned.startswith(word + " "):
            return False
    return None


def _pref_command(transcript: str) -> str | None:
    """Return the voice command when the *whole* utterance is one, otherwise None."""
    cleaned = " ".join(
        (transcript or "").lower().replace("!", "").replace(".", "").replace(",", "").replace("?", "").split()
    )
    if not cleaned or len(cleaned) > 60:
        return None
    core = cleaned
    for _ in range(3):  # strip at most a few layers of "can you ... please"
        changed = False
        for filler in _PREF_FILLERS:
            if core.startswith(filler + " "):
                core = core[len(filler) + 1:].strip()
                changed = True
            if core.endswith(" " + filler):
                core = core[: -len(filler) - 1].strip()
                changed = True
        if not changed:
            break
    return _PREF_PHRASES.get(core)


class VoiceChatSession:
    """Owns one bot voice connection in a public channel and answers spoken questions."""

    def __init__(
        self,
        bot: discord.Client,
        channel: discord.VoiceChannel,
        answer_fn: Callable[[str, int], Awaitable[str]],
        answer_stream_fn: Optional[Callable[[str, int], AsyncIterator[str]]] = None,
        *,
        settings,
        memory_recorder: Optional[Callable[[int, str, str], Awaitable[None]]] = None,
        prefs: Any = None,
        idea_fn: Any = None,
        transcribe_fn: Optional[Callable[[Path], str]] = None,
        speak_fn: Optional[Callable[[str, Path], Awaitable[None]]] = None,
    ):
        self.bot = bot
        self.channel = channel
        self.settings = settings
        self._answer_fn = answer_fn
        self._answer_stream_fn = answer_stream_fn
        # Durable memory hook: the bot wires this to the message archive.
        self._memory_recorder = memory_recorder
        # Barge-in: a human talking wins over the bot talking.
        self._speaking = False
        self._stop_requested = False
        self._prefs = prefs
        # A spoken idea waits here until the same member says yes (or the window passes).
        self._pending_idea: tuple[int, str, float] | None = None
        self._idea_fn = idea_fn
        self._transcribe_fn = transcribe_fn
        self._speak_fn = speak_fn
        self.vc: Optional[discord.VoiceClient] = None
        self.receiver: Optional[VoiceReceiver] = None
        self._queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(maxsize=max(1, settings.voice_max_queue))
        self._worker: Optional[asyncio.Task] = None
        self._poller: Optional[asyncio.Task] = None
        self._last_spoke: dict[int, float] = {}
        self._dropped: int = 0
        # Spoken follow-ups ("what about the second one?") only work if the session remembers what was
        # asked and answered out loud; a voice channel carries no text history of its own.
        self._history: list[tuple[str, str]] = []
        # Per-guild serialisation: one answer at a time, never interleaved.
        self._answer_lock = asyncio.Lock()
        self.joined_at: Optional[float] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._stop = False

    def history_block(self) -> str:
        """The last few spoken exchanges, formatted for the prompt (empty on a first question)."""
        if not self._history:
            return ""
        lines: list[str] = []
        for question, answer in self._history[-3:]:
            lines.append(f"- They asked out loud: {question[:200]}")
            if answer:
                lines.append(f"  You answered out loud: {answer[:200]}")
        return "\n".join(lines)[:900]

    async def remember(self, question: str, answer: str, *, user_id: int = 0) -> None:
        """Keep an exchange in the session and, when wired, in the durable memory archive."""
        if not question.strip():
            return
        self._history.append((question.strip(), (answer or "").strip()))
        del self._history[:-5]
        if self._memory_recorder is not None and user_id:
            try:
                await self._memory_recorder(int(user_id), question.strip(), (answer or "").strip())
            except Exception as exc:  # memory trouble must never cost an answer
                _log(f"could not archive the voice exchange: {type(exc).__name__}: {exc}")

    # --- lifecycle ---

    def _apply_dave_mode(self) -> None:
        """Advertise no E2EE unless asked otherwise, so members' audio arrives decryptable.

        davey cannot decrypt a speaker's frames even when the MLS group lists them, so with E2EE on the bot hears
        noise, the recogniser returns nothing and it never answers. Discord downgrades the channel to
        transport-only encryption when one participant does not support E2EE, which is the path that works.
        """
        mode = str(getattr(self.settings, "voice_dave_mode", "off") or "off").lower()
        if mode != "off":
            return
        try:
            import discord.voice_state as voice_state

            if getattr(voice_state, "has_dave", False):
                voice_state.has_dave = False  # max_dave_protocol_version() now reports 0
                _log("voice E2EE disabled for this session: the channel falls back to unencrypted audio")
        except Exception as exc:
            _log(f"could not disable voice E2EE: {type(exc).__name__}: {exc}")

    def humans_present(self) -> list[discord.Member]:
        """Non-bot members currently in the voice channel."""
        return [m for m in self.channel.members if not m.bot]

    async def start(self) -> bool:
        if not self.humans_present():
            _log("nobody is in the voice channel right now; waiting for someone to join")
            return False
        _log(f"joining #{self.channel} (id={self.channel.id})")
        self._apply_dave_mode()
        self.vc = await self.channel.connect(self_deaf=False, self_mute=False)
        self.joined_at = time.time()
        self.receiver = VoiceReceiver(self.vc, allowed_user_ids=None)
        self.receiver.start()
        _log(f"connected: gateway={getattr(self.vc, 'endpoint', '?')} region={getattr(self.vc, 'region', '?')}")
        _log(
            f"receiver started (bot_ssrc={self.receiver._bot_ssrc}, "
            f"key={len(self.receiver._current_keys()[0])}B, "
            f"dave={'yes' if self.receiver._current_keys()[1] else 'no'} "
            f"ready={bool(getattr(self.receiver._current_keys()[1], 'ready', False))})"
        )
        self._poller = asyncio.create_task(self._poll_loop(), name="chaosx-voice-poll")
        self._worker = asyncio.create_task(self._work_loop(), name="chaosx-voice-work")
        self._watchdog = asyncio.create_task(self._watch_loop(), name="chaosx-voice-watch")
        asyncio.create_task(self._greet_when_ready(), name="chaosx-voice-greet")
        _log(f"listening in #{self.channel.name} — idle costs nothing, models load on first speech")
        return True

    async def stop(self) -> None:
        self._stop = True
        for task in (self._poller, self._worker, self._watchdog):
            if task:
                task.cancel()
        if self.receiver:
            self.receiver.stop()
        if self.vc and self.vc.is_connected():
            await self.vc.disconnect()
        logger.info("ChaosX voice chat stopped")

    async def _greet_when_ready(self) -> None:
        """Wait for the end-to-end encryption handshake, then say hello.

        Speaking on join gives the user immediate proof the bot is present and audible, instead of
        wondering whether it is deaf for the first few seconds.
        """
        for _ in range(24):
            session = self.receiver._current_keys()[1] if self.receiver else None
            if session is None or getattr(session, "ready", False):
                break
            await asyncio.sleep(0.5)
        try:
            work = Path(self.settings.voice_work_dir)
            work.mkdir(parents=True, exist_ok=True)
            out = work / "greeting.wav"
            if self._speak_fn:
                await self._speak_fn("ChaosX is listening.", out)
                await self._play(out)
                _log("greeting spoken — both directions are live")
        except Exception as exc:
            _log(f"greeting failed: {type(exc).__name__}: {exc}")

    async def uptime_seconds(self) -> float:
        """How long the bot has been sitting in the voice channel."""
        return max(0.0, time.time() - self.joined_at) if self.joined_at else 0.0

    async def _watch_loop(self) -> None:
        """Discord occasionally drops a voice client; rejoin instead of sitting silently deaf."""
        last_heartbeat = 0.0
        while not self._stop:
            await asyncio.sleep(10)
            if time.time() - last_heartbeat >= 300:
                last_heartbeat = time.time()
                try:
                    import resource

                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
                except Exception:
                    rss_mb = 0.0
                loaded = []
                if getattr(self, "_stt_loaded", False):
                    loaded.append("stt")
                if getattr(self, "_tts_loaded", False):
                    loaded.append("tts")
                stats = self.receiver.stats() if self.receiver else {}
                active = {k: v for k, v in stats.items() if v}
                if self.receiver and getattr(self.receiver, "_dave_session", None):
                    session = self.receiver._dave_session
                    active["dave_ready"] = int(bool(getattr(session, "ready", False)))
                    active["dave_status"] = str(getattr(session, "status", "?"))
                    active["dave_epoch"] = str(getattr(session, "epoch", "?"))
                    try:
                        active["dave_keys"] = len(list(session.get_user_ids()))
                    except Exception as exc:
                        active["dave_keys"] = type(exc).__name__
                _log(
                    f"idle guarantee: no model calls while nobody speaks; models loaded: "
                    f"{','.join(loaded) or 'none'}; rss={rss_mb:.0f}MB; "
                    f"audio counters: {active or 'nothing received yet'}"
                )
            try:
                if self.vc and self.vc.is_connected():
                    if not self.humans_present():
                        _log("channel is empty; leaving voice so the channel shows as inactive")
                        await self.stop()
                        return
                    continue
                _log("voice connection lost; rejoining")
                if self.receiver:
                    self.receiver.stop()
                if self.vc:
                    try:
                        await self.vc.disconnect(force=True)
                    except Exception:
                        pass
                self.vc = await self.channel.connect(self_deaf=False, self_mute=False, reconnect=True)
                self.joined_at = time.time()
                self.receiver = VoiceReceiver(self.vc, allowed_user_ids=None)
                self.receiver.start()
                _log("rejoined voice channel")
            except Exception as exc:
                _log(f"rejoin failed ({type(exc).__name__}: {exc}); retrying in 15s")
                await asyncio.sleep(15)

    async def _poll_loop(self) -> None:
        while True:
            try:
                # Barge-in: someone really talking while the bot talks ends the bot's audio immediately.
                # A breath or a cough must not cut the bot off (it also must not queue as a question).
                if self._speaking and (
                    self.receiver.speech_like_incoming() if self.receiver else self.anyone_speaking()
                ):
                    self.stop_talking("someone started talking")
                for user_id, pcm in self.receiver.check_silence() if self.receiver else []:
                    try:
                        self._queue.put_nowait((user_id, pcm))
                    except asyncio.QueueFull:
                        self._dropped += 1
            except Exception as exc:
                _log(f"poll loop error: {type(exc).__name__}: {exc}")
            await asyncio.sleep(0.25)

    async def _work_loop(self) -> None:
        while True:
            user_id, pcm = await self._queue.get()
            try:
                await self._handle(user_id, pcm)
            except Exception as exc:
                _log(f"handling failed: {type(exc).__name__}: {exc}")
            finally:
                self._queue.task_done()

    # --- rate limiting, shared by every public speaker ---

    def _rate_limited(self, user_id: int) -> Optional[str]:
        now = time.monotonic()
        cooldown = max(0, int(self.settings.voice_cooldown_seconds))
        last = self._last_spoke.get(user_id, 0.0)
        if cooldown and now - last < cooldown:
            return f"please wait {int(cooldown - (now - last))}s"
        limit = self.settings.public_ask_limit_per_hour
        if limit > 0:
            window = self._hour_window(user_id)
            if len(window) >= limit:
                return "hourly question limit reached"
        return None

    def _hour_window(self, user_id: int) -> deque:
        now = time.monotonic()
        window = self._windows.setdefault(user_id, deque())
        while window and now - window[0] > 3600:
            window.popleft()
        return window

    _windows: dict[int, deque] = {}

    async def _handle(self, user_id: int, pcm: bytes) -> None:
        reason = self._rate_limited(user_id)
        if reason:
            logger.info("voice: ignoring user %s (%s)", user_id, reason)
            return
        self._last_spoke[user_id] = time.monotonic()
        # The hourly question budget is spent just before the model call (see below), so commands and
        # ideas - which never reach the model - do not consume it.

        work = Path(self.settings.voice_work_dir)
        work.mkdir(parents=True, exist_ok=True)
        wav = pcm_to_wav(pcm, work / f"utt-{user_id}-{int(time.time())}.wav")
        usable, detail = self._audio_looks_usable(pcm)
        _log(f"utterance from user {user_id} ({len(pcm) / BYTES_PER_SECOND:.1f}s; {detail})")
        if not usable:
            _log("discarding utterance: nothing intelligible was captured")
            return
        question = await asyncio.to_thread(self._transcribe, wav)
        if not question.strip():
            _log("recogniser returned nothing on the filtered pass; retrying unfiltered")
            question = await asyncio.to_thread(self._transcribe, wav, True)
        _log(f"heard: {question[:140]!r}")
        self._keep_audio(wav)
        if looks_like_prompt_echo(question):
            _log("that transcript was only prompt words, not speech; ignoring it")
            return
        # "hi" is two characters: a length gate of 3 silently dropped ordinary greetings.
        if not question or len(question.strip()) < 2:
            return
        command = _pref_command(question)
        if command == "stop":
            _log("asked to stop; cutting the current answer")
            self.stop_talking("asked to stop")
            return
        if command is not None and self._prefs is not None:
            confirmation = self._apply_pref_command(command, user_id)
            _log(f"voice preference applied: {command}")
            if confirmation:
                async with self._answer_lock:
                    await self._say(confirmation, user_id)
            return
        if self._pending_idea is not None and self._pending_idea[0] == user_id:
            pending_user, pending_text, expires = self._pending_idea
            if time.time() < expires:
                verdict = _confirmation(question)
                if verdict is True:
                    self._pending_idea = None
                    _log("spoken idea confirmed; filing it")
                    filed = ""
                    try:
                        filed = await self._idea_fn(user_id, pending_text) if self._idea_fn else ""
                    except Exception as exc:
                        _log(f"filing the spoken idea failed: {type(exc).__name__}: {exc}")
                    async with self._answer_lock:
                        await self._say(
                            filed or "I could not file that idea just now - try again in a moment.", user_id
                        )
                    return
                if verdict is False:
                    self._pending_idea = None
                    _log("spoken idea cancelled by the member")
                    async with self._answer_lock:
                        await self._say("Cancelled.", user_id)
                    return
            else:
                self._pending_idea = None  # our own idea, gone stale

        idea_attempt, idea_body = _idea_request(question)
        if idea_attempt:
            if self._idea_fn is None:
                _log("heard an idea but no filing callback is wired")
                async with self._answer_lock:
                    await self._say("I cannot file ideas right now - try again in a moment.", user_id)
                return
            if len(idea_body) < 12:
                _log("heard an idea with no body; asking for the detail")
                async with self._answer_lock:
                    await self._say("Tell me the idea in a few more words and I will file it.", user_id)
                return
            self._pending_idea = (user_id, idea_body, time.time() + 90)
            _log(f"spoken idea held for confirmation ({len(idea_body)} chars)")
            readback = idea_body if len(idea_body) <= 300 else idea_body[:300] + "..."
            async with self._answer_lock:
                await self._say(
                    f"Here is the idea I will submit: {readback} Say yes to send it, or no to cancel.",
                    user_id,
                )
            return

        # Past this point a real question is going to the model: that is what the hourly budget counts.
        if self.settings.public_ask_limit_per_hour > 0:
            self._hour_window(user_id).append(time.monotonic())

        self._stop_requested = False
        async with self._answer_lock:
            started = time.time()
            spoken_total = 0
            if self._answer_stream_fn is not None:
                spoken_parts: list[str] = []
                # Speak each sentence as it arrives: the bot starts talking while the model writes.
                async for sentence in self._answer_stream_fn(question, user_id, self.history_block()):
                    spoken = speech_safe(self._trim_for_voice(sentence))
                    if not spoken:
                        continue
                    _log(f"speaking ({len(spoken)} chars, first at {time.time() - started:.1f}s): {spoken[:110]!r}")
                    if not getattr(self, "_tts_loaded", False):
                        self._tts_loaded = True
                        _log("loading text-to-speech voice (first answer)")
                    out = work / f"ans-{int(time.time() * 1000)}.wav"
                    if self._speak_fn:
                        pace = (
                            self._prefs.pace(user_id, self.settings.voice_tts_length_scale)
                            if self._prefs
                            else None
                        )
                        await self._speak_fn(spoken, out, pace)
                    await self._play(out)
                    if self._stop_requested:
                        _log("answer cut short at the speaker's request")
                        await self.remember(question, " ".join(spoken_parts), user_id=user_id)
                        return
                    spoken_total += len(spoken)
                    spoken_parts.append(spoken)
                    if spoken_total >= max(80, int(self.settings.voice_max_answer_chars)):
                        await self.remember(question, " ".join(spoken_parts), user_id=user_id)
                        return
                if spoken_total == 0:
                    _log("nothing to say for that utterance")
                else:
                    await self.remember(question, " ".join(spoken_parts), user_id=user_id)
                return
            answer = await self._answer_fn(question, user_id, self.history_block())
            if not answer:
                return
            spoken = speech_safe(self._trim_for_voice(answer))
            await self.remember(question, spoken, user_id=user_id)
            _log(f"answering ({len(spoken)} chars): {spoken[:110]!r}")
            out = work / f"ans-{int(time.time())}.wav"
            if self._speak_fn:
                pace = (
                    self._prefs.pace(user_id, self.settings.voice_tts_length_scale) if self._prefs else None
                )
                await self._speak_fn(spoken, out, pace)
            await self._play(out)
            await self.remember(question, spoken, user_id=user_id)

    @staticmethod
    def _audio_looks_usable(pcm: bytes) -> tuple[bool, str]:
        """Reject decode garbage (near full scale) and near-silence before spending STT on it."""
        import audioop
        import math

        try:
            rms = audioop.rms(pcm, 2)
            peak = audioop.max(pcm, 2)
        except Exception:
            return False, "unreadable audio"
        rms_dbfs = 20 * math.log10(rms / 32768) if rms else float("-inf")
        peak_dbfs = 20 * math.log10(peak / 32768) if peak else float("-inf")
        if rms_dbfs > -6:
            return False, f"looks like undecrypted data (rms {rms_dbfs:.1f} dBFS, peak {peak_dbfs:.1f} dBFS)"
        if rms_dbfs < -55:
            return False, f"effectively silent (rms {rms_dbfs:.1f} dBFS)"
        return True, f"rms {rms_dbfs:.1f} dBFS, peak {peak_dbfs:.1f} dBFS"

    @staticmethod
    def _level_dbfs(pcm: bytes) -> float:
        """Peak level of a PCM buffer in dBFS (-inf for silence), for diagnosing dead audio."""
        import audioop

        try:
            peak = audioop.max(pcm, 2)
        except Exception:
            return float("-inf")
        if peak <= 0:
            return float("-inf")
        import math

        return round(20 * math.log10(peak / 32768), 1)

    def _keep_audio(self, wav: Path) -> None:
        """Keep the last few utterances so a bad transcript can be re-examined."""
        try:
            keep = Path(self.settings.voice_work_dir) / "keep"
            keep.mkdir(parents=True, exist_ok=True)
            target = keep / wav.name
            wav.replace(target)
            files = sorted(keep.glob("*.wav"), key=lambda p: p.stat().st_mtime)
            for old in files[:-3]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    def _trim_for_voice(self, text: str) -> str:
        limit = max(80, int(self.settings.voice_max_answer_chars))
        clean = " ".join(text.split())
        if len(clean) <= limit:
            return clean
        cut = clean[:limit]
        for stop in (". ", "! ", "? "):
            idx = cut.rfind(stop)
            if idx > limit // 2:
                return cut[: idx + 1]
        return cut.rstrip() + "…"

    def _transcribe(self, wav: Path, unfiltered: bool = False) -> str:
        if self._transcribe_fn is None:
            return ""
        if not getattr(self, "_stt_loaded", False):
            self._stt_loaded = True
            _log("loading speech-to-text model (first utterance)")
        try:
            return self._transcribe_fn(wav, unfiltered)
        except TypeError:
            return self._transcribe_fn(wav)

    async def _play(self, wav: Path) -> None:
        assert self.vc is not None
        if self._stop_requested:
            return
        source = discord.FFmpegPCMAudio(str(wav))
        done = asyncio.Event()
        self._speaking = True
        try:
            self.vc.play(source, after=lambda _exc: self.bot.loop.call_soon_threadsafe(done.set))
            await done.wait()
        finally:
            self._speaking = False

    def anyone_speaking(self) -> bool:
        """True while a human is sending audio (their utterance buffer is filling)."""
        if self.receiver is None:
            return False
        try:
            return any(len(buf) > 0 for buf in self.receiver._buffers.values())
        except Exception:
            return False

    def stop_talking(self, reason: str) -> bool:
        """Cut the bot's own audio immediately: a human started talking, or asked it to stop."""
        self._stop_requested = True
        stopped = False
        if self.vc is not None and self._speaking:
            try:
                self.vc.stop()
                stopped = True
            except Exception:
                pass
        if stopped:
            _log(f"stopped talking ({reason})")
        return stopped

    async def _say(self, line: str, user_id: int) -> None:
        """Speak a short fixed line (a preference confirmation), without calling the model."""
        work = Path(self.settings.voice_work_dir)
        work.mkdir(parents=True, exist_ok=True)
        out = work / f"say-{int(time.time() * 1000)}.wav"
        self._stop_requested = False
        pace = self._prefs.pace(user_id, self.settings.voice_tts_length_scale) if self._prefs else None
        if self._speak_fn:
            await self._speak_fn(line, out, pace)
        await self._play(out)

    def _apply_pref_command(self, command: str, user_id: int) -> str:
        """Apply a spoken preference change and return the line to say back."""
        if self._prefs is None:
            return ""
        try:
            if command == "reset":
                self._prefs.update(user_id, pace=None, verbosity=None)
                return "Back to my normal voice settings."
            if command in ("slower", "faster"):
                current = self._prefs.pace(user_id, self.settings.voice_tts_length_scale)
                step = 1.15 if command == "slower" else (1 / 1.15)
                new_pace = max(0.6, min(2.0, round(current * step, 3)))
                self._prefs.update(user_id, pace=new_pace)
                if abs(new_pace - current) < 0.01:
                    return "That is as " + ("slow" if command == "slower" else "fast") + " as I go."
                return "Slower it is." if command == "slower" else "Faster it is."
            if command == "short":
                self._prefs.update(user_id, verbosity="short")
                return "Short answers from now on."
            if command == "detailed":
                self._prefs.update(user_id, verbosity="detailed")
                return "I will go into more detail from now on."
        except Exception as exc:
            _log(f"could not apply preference {command}: {type(exc).__name__}: {exc}")
        return ""
