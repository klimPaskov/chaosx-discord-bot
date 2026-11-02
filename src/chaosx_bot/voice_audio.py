"""Speech-to-text and text-to-speech helpers for the public voice channel.

Two shapes of host are supported, chosen by ``build_stt``/``build_tts``:

* **Big host** (VPS/A1): local models, both lazy and CPU-only — faster-whisper for speech
  recognition and Piper for speech synthesis. Piper costs nothing per word and runs ~11x faster
  than real time there, which keeps a public channel free to operate.
* **1/8-OCPU host** (the free Micro): the same local models measured 0.24x realtime (Whisper
  ``base``) and 0.2x realtime (Piper medium) — a 3 s question took 12 s to transcribe, so a live
  channel is impossible. There the recogniser is Groq's hosted Whisper (key: ``GROQ_API_KEY``) and
  the voice is Edge's free neural set, which keeps the box to moving bytes.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

# Used ONLY on the retry pass, when the plain pass returned nothing at all. A prompt biases the
# decoder, which is exactly what we want on an inaudible utterance and exactly what hurts on a short
# one: with the prompt active, a real 2.2s "hi" came back as "Super events.".
STT_PROMPT = "Chaos Redux. Super events. HOI4 mod."

# The few words this project's vocabulary actually loses. Deterministic, so unlike a prompt it can
# never invent content: applied to the transcript after the fact.
STT_CORRECTIONS = (
    ("case you're ducks", "Chaos Redux"),
    ("case your ducks", "Chaos Redux"),
    ("chaos reducks", "Chaos Redux"),
    ("chaos reduce", "Chaos Redux"),
    ("chaos redux", "Chaos Redux"),
    ("supervent", "super event"),
    ("supervents", "super events"),
    ("super events", "super events"),
    ("super event", "super event"),
    ("hoi 4", "HOI4"),
    ("hoi for", "HOI4"),
    # Proper nouns the recogniser mangles on real (and synthetic) speech. Each key is a string that
    # cannot occur in a genuine Chaos Redux question, so a replacement can only fix, never invent.
    ("krumbeneath", "crown beneath"),
    ("crumb beneath", "crown beneath"),
    ("crum beneath", "crown beneath"),
    ("crown beneath", "crown beneath"),
    ("black plates", "black plague"),
    ("blackplace", "black plague"),
    ("black plague", "black plague"),
    ("black plage", "black plague"),
    ("hoops mccain", "Hoops McCann"),
    ("hoops mccaan", "Hoops McCann"),
    ("hoops mccan", "Hoops McCann"),
    ("hoops mccann", "Hoops McCann"),
    ("atomic zombie league", "Anti-Zombie League"),
    ("anti-zombie league", "Anti-Zombie League"),
    ("anti zombie league", "Anti-Zombie League"),
    ("kr6", "ChaosX"),
    ("ksx", "ChaosX"),
    ("hoi4 mod", "HOI4 mod"),
)


def _correct_domain_terms(text: str) -> str:
    """Repair the handful of domain words the recogniser reliably mangles."""
    lowered = text.lower()
    for wrong, right in STT_CORRECTIONS:
        if wrong in lowered:
            start = 0
            while True:
                index = lowered.find(wrong, start)
                if index < 0:
                    break
                text = text[:index] + right + text[index + len(wrong) :]
                lowered = text.lower()
                start = index + len(right)
    return text

# Domain words the recogniser otherwise mangles ("Zombie Apocalypse" came back as
# "Somia-bockelets"): bias the model toward the vocabulary we actually expect.
GLOSSARY = (
    "Chaos Redux, super event, epilogue, epigraph, scenario, focus tree, Hearts of Iron IV, HOI4, "
    "mod, playtest, devlog, zombie outbreak, anti-zombie league, testing poll, community suggestion, tier."
)


class SpeechToText:
    """Local faster-whisper transcription (no per-minute API cost)."""

    def __init__(self, model_size: str = "base"):
        self.model_size = model_size
        self._model = None
        self._lock = Lock()

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            logger.info("voice STT ready (faster-whisper %s)", self.model_size)
        return self._model

    def transcribe(self, wav_path: Path, *, vad_filter: bool = True, prompt: bool = False) -> str:
        model = self._ensure_model()
        with self._lock:
            segments, _info = model.transcribe(
                str(wav_path), beam_size=1, vad_filter=vad_filter,
                language="en",
                initial_prompt=STT_PROMPT if prompt else None,
                condition_on_previous_text=False,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
        return _correct_domain_terms(" ".join(text.split()))


class TextToSpeech:
    """Local Piper synthesis with an explicit voice and pacing."""

    def __init__(self, voice_path: str, speaker: str = "", length_scale: float = 1.30):
        self.voice_path = voice_path
        self.speaker = speaker
        self.length_scale = length_scale
        self._voice = None
        self._speaker_id: int | None = None

    def _ensure_voice(self):
        if self._voice is None:
            from piper import PiperVoice

            path = Path(self.voice_path)
            config = path.with_suffix(path.suffix + ".json")
            self._voice = PiperVoice.load(str(path), config_path=str(config))
            if self.speaker:
                mapping = json.loads(config.read_text(encoding="utf-8")).get("speaker_id_map", {})
                if self.speaker not in mapping:
                    raise KeyError(f"voice has no speaker {self.speaker!r}; have {sorted(mapping)[:8]}")
                self._speaker_id = int(mapping[self.speaker])
            logger.info("voice TTS ready (%s speaker=%s pace=%.2f)", path.name, self.speaker, self.length_scale)
        return self._voice

    def render(self, text: str, out_wav: Path, length_scale: float | None = None) -> Path:
        """Synthesize ``text`` to ``out_wav`` (48 kHz mono) and return the path.

        ``length_scale`` overrides the configured pace for one caller (per-user voice preferences).
        """
        import wave

        from piper import SynthesisConfig

        voice = self._ensure_voice()
        synth = SynthesisConfig(
            speaker_id=self._speaker_id,
            length_scale=float(length_scale) if length_scale else self.length_scale,
        )
        raw = Path(tempfile.mkstemp(suffix=".wav", dir=str(out_wav.parent))[1])
        with wave.open(str(raw), "wb") as w:
            voice.synthesize_wav(text, w, syn_config=synth)
        # Discord plays best from 48 kHz mono; resample and drop the extra channel.
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw),
             "-ar", "48000", "-ac", "1", str(out_wav)],
            check=True,
        )
        raw.unlink(missing_ok=True)
        return out_wav


class GroqSpeechToText:
    """Hosted Whisper through Groq's OpenAI-compatible endpoint.

    Used on a host whose CPU cannot run Whisper in real time. The request is a plain multipart
    POST so no SDK is needed; every failure falls back to a local model when one is available,
    because a voice channel that goes silent on a network blip reads as "the bot is broken".
    """

    ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(
        self,
        api_key: str,
        model: str = "whisper-large-v3-turbo",
        *,
        fallback: SpeechToText | None = None,
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.model = model
        self.fallback = fallback
        self.timeout = timeout

    def transcribe(self, wav_path: Path, *, vad_filter: bool = True, prompt: bool = False) -> str:
        try:
            text = self._request(Path(wav_path), prompt=prompt)
        except Exception as exc:  # network, quota, bad key: never cost the channel its answer
            if self.fallback is None:
                raise
            logger.warning("voice STT cloud call failed (%s); using the local model instead", exc)
            return self.fallback.transcribe(wav_path, vad_filter=vad_filter, prompt=prompt)
        return _correct_domain_terms(" ".join(text.split()))

    def _request(self, wav_path: Path, *, prompt: bool) -> str:
        boundary = f"----chaosx{secrets.token_hex(12)}"
        fields = {
            "model": self.model,
            "language": "en",
            "response_format": "json",
            "temperature": "0",
        }
        if prompt:
            fields["prompt"] = STT_PROMPT
        body = bytearray()
        for name, value in fields.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode("utf-8")
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{wav_path.name}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        body += wav_path.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        request = urllib.request.Request(
            self.ENDPOINT,
            data=bytes(body),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "chaosx-bot",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("text", "") or "")


class EdgeTextToSpeech:
    """Edge's free neural voices, for a host too slow to synthesize locally.

    Same ``render`` contract as the Piper ``TextToSpeech``: write a 48 kHz mono wav. ``length_scale``
    is translated to Edge's rate percentage (1.30 = 30% longer = -23% rate).
    """

    def __init__(
        self,
        voice: str = "en-GB-RyanNeural",
        length_scale: float = 1.30,
        *,
        timeout: float = 30.0,
    ):
        self.voice = voice
        self.length_scale = length_scale
        self.timeout = timeout

    def render(self, text: str, out_wav: Path, length_scale: float | None = None) -> Path:
        import asyncio

        import edge_tts

        pace = float(length_scale) if length_scale else self.length_scale
        rate = f"{round((1.0 / pace - 1.0) * 100):+d}%"
        mp3 = Path(tempfile.mkstemp(suffix=".mp3", dir=str(out_wav.parent))[1])

        async def _save() -> None:
            await edge_tts.Communicate(text, self.voice, rate=rate).save(str(mp3))

        try:
            asyncio.run(asyncio.wait_for(_save(), timeout=self.timeout))
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3),
                 "-ar", "48000", "-ac", "1", str(out_wav)],
                check=True,
            )
        finally:
            mp3.unlink(missing_ok=True)
        return out_wav


def _installed(module: str) -> bool:
    try:
        __import__(module)
    except Exception:
        return False
    return True


def build_stt(settings):
    """Pick the recogniser this host can actually run in real time.

    ``auto`` -> hosted Whisper when ``GROQ_API_KEY`` is present, else the local model. A local model
    is always kept as the cloud fallback so a Groq outage degrades latency instead of answers.
    """
    provider = str(getattr(settings, "voice_stt_provider", "auto") or "auto").strip().lower()
    local = SpeechToText(settings.voice_stt_model)
    if provider in ("auto", "groq"):
        key = os.getenv("GROQ_API_KEY", "").strip()
        if key:
            model = getattr(settings, "voice_stt_groq_model", "whisper-large-v3-turbo")
            logger.info("voice STT: hosted Whisper (%s) with local %s fallback", model, settings.voice_stt_model)
            return GroqSpeechToText(key, model, fallback=local if _installed("faster_whisper") else None)
        if provider == "groq":
            logger.warning("voice STT provider=groq but GROQ_API_KEY is empty; falling back to local Whisper")
    return local


def build_tts(settings):
    """Pick the synthesizer: Edge's free voices when installed, else local Piper."""
    provider = str(getattr(settings, "voice_tts_provider", "auto") or "auto").strip().lower()
    if provider == "edge" or (provider == "auto" and _installed("edge_tts")):
        return EdgeTextToSpeech(
            getattr(settings, "voice_tts_edge_voice", "en-GB-RyanNeural"),
            length_scale=settings.voice_tts_length_scale,
        )
    return TextToSpeech(
        settings.voice_tts_voice,
        speaker=os.getenv("CHAOSX_VOICE_TTS_SPEAKER", settings.voice_tts_speaker),
        length_scale=settings.voice_tts_length_scale,
    )
