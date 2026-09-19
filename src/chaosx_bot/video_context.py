"""Video attachments/links → sampled frames + speech transcript for ChaosX.

Discord video attachments (and direct video links) are turned into model input:

* N evenly spaced frames as PNG data URIs, so the vision model can actually look
  at what happens in the clip;
* a speech transcript from a local faster-whisper model (when installed),
  injected as text.

Everything is bounded (bytes, duration, frame count, transcript seconds) and
best-effort: any failure degrades to a plain acknowledgement of the file, never
an exception or a silent drop. ffmpeg/ffprobe are already a dependency of the
image-attachment path.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
FFMPEG_TIMEOUT_S = 90.0
STT_SAMPLE_RATE = 16_000

VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".ts", ".3gp", ".ogv"}
)

# Hosts whose pages are video platforms (a direct file fetch won't work; yt-dlp
# is the route). Kept deliberately small and obvious.
VIDEO_PLATFORM_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "streamable.com",
    "vimeo.com",
    "twitch.tv",
    "clips.twitch.tv",
    "medal.tv",
    "kick.com",
)

_PRIVATE_HOST_RE = re.compile(
    r"^(?:localhost|0\.0\.0\.0|127\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|169\.254\.|\[?::1\]?|.*\.local)$",
    re.IGNORECASE,
)

def _head(data: bytes, size: int = 32) -> bytes:
    return data[:size]


def looks_like_video(data: bytes, *, ext: str = "", content_type: str = "") -> bool:
    """True when the bytes/extension/content-type describe a video container.

    Magic bytes decide whenever they are conclusive; the extension is only
    trusted together with a video content type, so a mislabelled text file
    cannot drag the video pipeline in.
    """
    head = _head(data)
    # MP4/MOV/M4V family: an `ftyp` box at offset 4. Known brands first, but the
    # box alone is already strong evidence (some muxers write uncommon brands).
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True
    if head.startswith(b"\x1aE\xdf\xa3"):  # Matroska / WebM
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return True
    if head.startswith(b"\x00\x00\x01\xba") or head.startswith(b"\x00\x00\x01\xb3"):  # MPEG PS
        return True
    if head.startswith(b"FLV\x01"):
        return True
    ctype = (content_type or "").lower().strip()
    if ctype.startswith("video/"):
        return True
    return bool(ext) and ext.lower() in VIDEO_EXTENSIONS and "video" in ctype


def is_video_link(url: str) -> bool:
    """True for a URL that should go through the video pipeline."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if any(host == platform or host.endswith("." + platform) for platform in VIDEO_PLATFORM_HOSTS):
        return True
    suffix = Path(parsed.path).suffix.lower()
    return suffix in VIDEO_EXTENSIONS


def format_duration(seconds: float) -> str:
    """Compact mm:ss / h:mm:ss clock for prompts."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def frame_timestamps(duration: float, count: int) -> list[float]:
    """Evenly spaced sample points inside the clip (never the exact first/last frame)."""
    if count <= 0:
        return []
    if duration <= 0:
        return [0.0 for _ in range(count)]
    span = max(0.0, duration)
    # Sample at (i + 0.5)/count of the clip so we avoid black leader/tail frames.
    return [round(span * (index + 0.5) / count, 3) for index in range(count)]


def format_video_block(
    *,
    label: str,
    probe: dict[str, Any],
    transcript: str = "",
    transcript_note: str = "",
    frames: int = 0,
    timestamps: list[float] | None = None,
    truncated: bool = False,
) -> str:
    """Prompt-ready description of one processed video.

    The block is explicit about what was extracted so the model never claims it
    watched the file itself, and so a missing transcript reads as a stated
    limitation instead of an invented summary.
    """
    duration = float(probe.get("duration") or 0.0)
    bits = [f"length {format_duration(duration)}" if duration else "length unknown"]
    if probe.get("width") and probe.get("height"):
        bits.append(f"{int(probe['width'])}x{int(probe['height'])}")
    bits.append("audio: yes" if probe.get("has_audio") else "audio: none")
    if probe.get("has_video") is False:
        bits.append("video stream: none")
    header = f"## Attached video: {label} ({', '.join(bits)})"
    if truncated:
        header += f"\n(only the first {format_duration(probe.get('processed_seconds') or 0)} were analysed)"
    lines = [header]
    if frames and timestamps:
        stamps = ", ".join(format_duration(value) for value in timestamps)
        lines.append(
            f"{frames} frame(s) sampled at {stamps} are attached as images in that order — "
            "describe and reason about what they show."
        )
    if transcript.strip():
        lines.append("Speech transcript (automatic, may contain errors):")
        lines.append('"""')
        lines.append(transcript.strip())
        lines.append('"""')
    elif transcript_note:
        lines.append(f"Speech transcript: {transcript_note}")
    else:
        lines.append("Speech transcript: none (the clip has no speech audio).")
    return "\n".join(lines)


async def _run(cmd: list[str], *, timeout: float = FFMPEG_TIMEOUT_S) -> tuple[int, bytes, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.communicate()
        return 124, b"", f"timeout after {timeout}s"
    return proc.returncode or 0, out, err.decode("utf-8", errors="replace")


def ffmpeg_available() -> bool:
    return bool(shutil.which(FFMPEG) and shutil.which(FFPROBE))


async def probe_video(path: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    """ffprobe the file: duration, dimensions, stream presence."""
    if not ffmpeg_available():
        return {}
    code, out, _ = await _run(
        [
            FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        ],
        timeout=timeout,
    )
    if code != 0 or not out:
        return {}
    try:
        data = json.loads(out.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"), (video or {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    return {
        "duration": duration,
        "width": (video or {}).get("width") or 0,
        "height": (video or {}).get("height") or 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "codec": (video or {}).get("codec_name") or "",
    }


async def extract_frames(
    path: Path,
    out_dir: Path,
    *,
    count: int = 4,
    width: int = 768,
    duration: float = 0.0,
    max_seconds: float | None = None,
) -> tuple[list[tuple[float, bytes]], list[float]]:
    """Extract `count` PNG frames; returns ([(timestamp, png_bytes)], timestamps)."""
    if not ffmpeg_available() or count <= 0:
        return [], []
    span = duration or 0.0
    if max_seconds is not None and span:
        span = min(span, max_seconds)
    stamps = frame_timestamps(span, count)
    frames: list[tuple[float, bytes]] = []
    kept: list[float] = []
    for index, stamp in enumerate(stamps):
        target = out_dir / f"frame{index:02d}.png"
        code, _, _ = await _run(
            [
                FFMPEG, "-v", "error", "-y",
                "-ss", f"{stamp:.3f}",
                "-i", str(path),
                "-frames:v", "1",
                "-vf", f"scale={int(width)}:-2:flags=lanczos",
                str(target),
            ]
        )
        if code != 0 or not target.exists():
            continue
        try:
            data = target.read_bytes()
        except OSError:
            continue
        if data:
            frames.append((stamp, data))
            kept.append(stamp)
    return frames, kept


async def extract_audio(path: Path, out_wav: Path, *, max_seconds: float, timeout: float = 120.0) -> bool:
    """Extract a mono 16 kHz WAV track (what speech models want)."""
    if not ffmpeg_available():
        return False
    cmd = [FFMPEG, "-v", "error", "-y", "-i", str(path)]
    if max_seconds > 0:
        cmd += ["-t", f"{max_seconds:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(STT_SAMPLE_RATE), "-f", "wav", str(out_wav)]
    code, _, _ = await _run(cmd, timeout=timeout)
    return code == 0 and out_wav.exists() and out_wav.stat().st_size > 44


# --- local speech-to-text (faster-whisper) ---------------------------------

_MODELS: dict[str, Any] = {}


def stt_available() -> bool:
    """True when a local speech model implementation is importable."""
    return importlib.util.find_spec("faster_whisper") is not None


def _load_model(model: str) -> Any:
    from faster_whisper import WhisperModel  # imported lazily: optional dependency

    instance = _MODELS.get(model)
    if instance is None:
        instance = WhisperModel(model, device="cpu", compute_type="int8")
        _MODELS[model] = instance
    return instance


def _transcribe_sync(wav: Path, *, model: str, language: str) -> str:
    instance = _load_model(model)
    segments, _info = instance.transcribe(
        str(wav),
        language=language or None,
        vad_filter=True,
        beam_size=1,
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments if segment.text and segment.text.strip()).strip()


async def transcribe(
    wav: Path,
    *,
    model: str = "base",
    language: str = "en",
    timeout: float = 300.0,
) -> tuple[str, str]:
    """Transcribe a WAV file. Returns (text, note) — note explains a skip/failure."""
    if not wav.exists() or wav.stat().st_size <= 44:
        return "", "no audio track found"
    if not stt_available():
        return "", "skipped (no local speech model installed on the bot host)"
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_transcribe_sync, wav, model=model, language=language),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return "", f"skipped (speech model exceeded {int(timeout)}s)"
    except Exception as exc:  # noqa: BLE001 - transcription must never break an answer
        return "", f"skipped ({type(exc).__name__})"
    if not text.strip():
        return "", "no speech detected"
    return text.strip(), ""


# --- link sources ----------------------------------------------------------


async def download_video(url: str, *, max_bytes: int, timeout: float = 120.0) -> tuple[bytes, str] | None:
    """Download a direct video file URL (size-capped). None for anything else."""
    import aiohttp

    host = (urlparse(url).hostname or "").lower()
    if not host or _PRIVATE_HOST_RE.search(host):
        return None
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ChaosX/1.0)"}) as response:
                if response.status != 200:
                    return None
                content_type = (response.headers.get("Content-Type") or "").lower()
                length = int(response.headers.get("Content-Length") or 0)
                if length and length > max_bytes:
                    return None
                if not (content_type.startswith("video/") or content_type in ("application/octet-stream", "")):
                    # Some CDNs send binary content types; only trust video/* here
                    # unless the path itself looks like a video file.
                    if Path(urlparse(url).path).suffix.lower() not in VIDEO_EXTENSIONS:
                        return None
                body = await response.read()
    except Exception:
        return None
    if not body or len(body) > max_bytes or not looks_like_video(body, content_type=content_type):
        return None
    return body, content_type or "video/unknown"


def ytdlp_command() -> list[str] | None:
    """How to invoke yt-dlp: the installed module first, then a PATH binary.

    The bot's venv is not on PATH for the systemd unit, so a pip-installed
    yt-dlp console script is invisible to ``shutil.which`` — running the module
    through the bot's own interpreter is the reliable route.
    """
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    binary = shutil.which("yt-dlp")
    return [binary] if binary else None


def ytdlp_available() -> bool:
    return ytdlp_command() is not None


async def download_platform_video(
    url: str, out_dir: Path, *, max_seconds: int, timeout: float = 300.0
) -> Path | None:
    """Best-effort fetch of the first `max_seconds` of a platform video via yt-dlp.

    Datacenter IPs are often bot-walled by YouTube; failures are expected and
    simply mean the caller keeps whatever text context it already had.
    """
    launcher = ytdlp_command()
    if launcher is None or max_seconds <= 0:
        return None
    target = out_dir / "platform.mp4"
    cmd = [
        *launcher,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--retries", "1",
        "--socket-timeout", "20",
        "-f", "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]/b",
        "--download-sections", f"*0-{int(max_seconds)}",
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "-o", str(target),
        url,
    ]
    code, _, _ = await _run(cmd, timeout=timeout)
    return target if code == 0 and target.exists() and target.stat().st_size > 0 else None


def png_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def temp_workspace(prefix: str = "chaosx_video_") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}
