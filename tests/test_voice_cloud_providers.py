"""The cloud voice providers: request shape, reply handling, and host selection.

The free Micro runs 1/8 of an OCPU, where the local models measured 0.24x realtime (Whisper base)
and 0.2x realtime (Piper) — a 3 s question took 12 s to transcribe and 12 s to speak. These tests
pin the hosted path that replaces them there, without needing a Groq key or network.
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path

import pytest

from chaosx_bot import voice_audio
from chaosx_bot.voice_audio import (
    EdgeTextToSpeech,
    GroqSpeechToText,
    SpeechToText,
    build_stt,
    build_tts,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Settings:
    voice_stt_model = "base"
    voice_stt_provider = "auto"
    voice_stt_groq_model = "whisper-large-v3-turbo"
    voice_tts_provider = "auto"
    voice_tts_edge_voice = "en-GB-RyanNeural"
    voice_tts_voice = "/srv/chaosx/piper-voices/en_GB-vctk-medium.onnx"
    voice_tts_speaker = "p254"
    voice_tts_length_scale = 1.30


@pytest.fixture()
def wav(tmp_path: Path) -> Path:
    path = tmp_path / "utt-1.wav"
    path.write_bytes(b"RIFF" + b"0" * 64)
    return path


def _capture(monkeypatch, payload: dict, request_box: dict):
    def fake_urlopen(request, timeout=None):
        request_box["request"] = request
        request_box["timeout"] = timeout
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr(voice_audio.urllib.request, "urlopen", fake_urlopen)


def test_groq_request_is_multipart_with_model_and_key(monkeypatch, wav, tmp_path):
    box: dict = {}
    _capture(monkeypatch, {"text": "how do i get chaos redux"}, box)

    text = GroqSpeechToText("gsk-test", "whisper-large-v3-turbo").transcribe(wav)

    request = box["request"]
    assert request.full_url == GroqSpeechToText.ENDPOINT
    assert request.get_method() == "POST"
    assert request.headers["Authorization"] == "Bearer gsk-test"
    boundary = request.headers["Content-type"].split("boundary=")[1]
    body = request.data
    assert f'name="model"\r\n\r\nwhisper-large-v3-turbo'.encode() in body
    assert b'name="language"\r\n\r\nen' in body
    assert f'filename="{wav.name}"'.encode() in body
    assert wav.read_bytes() in body
    assert body.rstrip().endswith(f"--{boundary}--".encode())
    # "chaos redux" is a known correction pair; the transcript is repaired, not invented.
    assert text == "how do i get Chaos Redux"


def test_groq_sends_the_domain_prompt_only_on_the_retry_pass(monkeypatch, wav):
    box: dict = {}
    _capture(monkeypatch, {"text": "hi"}, box)
    stt = GroqSpeechToText("gsk-test")

    stt.transcribe(wav)
    assert b'name="prompt"' not in box["request"].data

    stt.transcribe(wav, prompt=True)
    assert voice_audio.STT_PROMPT.encode() in box["request"].data


def test_groq_falls_back_to_local_when_the_cloud_fails(monkeypatch, wav):
    def boom(*args, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(voice_audio.urllib.request, "urlopen", boom)

    class _Local:
        def __init__(self):
            self.calls = []

        def transcribe(self, path, *, vad_filter=True, prompt=False):
            self.calls.append((path, vad_filter, prompt))
            return "local answer"

    local = _Local()
    text = GroqSpeechToText("gsk-test", fallback=local).transcribe(wav, vad_filter=False, prompt=True)

    assert text == "local answer"
    assert local.calls == [(wav, False, True)]


def test_groq_without_a_fallback_still_raises(monkeypatch, wav):
    def boom(*args, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(voice_audio.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        GroqSpeechToText("gsk-test").transcribe(wav)


def test_edge_pace_becomes_a_rate_percentage(monkeypatch, tmp_path):
    seen: dict = {}

    class _Communicate:
        def __init__(self, text, voice, rate):
            seen.update(text=text, voice=voice, rate=rate)

        async def save(self, path):
            Path(path).write_bytes(b"ID3fake")
            seen["mp3"] = Path(path)

    fake = types.ModuleType("edge_tts")
    fake.Communicate = _Communicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake)
    monkeypatch.setattr(voice_audio.subprocess, "run", lambda *a, **k: seen.setdefault("ffmpeg", a[0]))

    out = tmp_path / "ans.wav"
    EdgeTextToSpeech("en-GB-RyanNeural", length_scale=1.30).render("hello there", out)

    assert seen["voice"] == "en-GB-RyanNeural"
    assert seen["rate"] == "-23%"
    assert "hello there" == seen["text"]
    assert seen["ffmpeg"][:4] == ["ffmpeg", "-y", "-hide_banner", "-loglevel"]
    assert "-ar" in seen["ffmpeg"] and "48000" in seen["ffmpeg"]
    assert not seen["mp3"].exists(), "the intermediate mp3 must not be left behind"


def test_edge_per_call_pace_override(monkeypatch, tmp_path):
    seen: dict = {}

    class _Communicate:
        def __init__(self, text, voice, rate):
            seen["rate"] = rate

        async def save(self, path):
            Path(path).write_bytes(b"ID3")

    fake = types.ModuleType("edge_tts")
    fake.Communicate = _Communicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake)
    monkeypatch.setattr(voice_audio.subprocess, "run", lambda *a, **k: None)

    EdgeTextToSpeech(length_scale=1.30).render("hi", tmp_path / "a.wav", length_scale=1.0)
    assert seen["rate"] == "+0%"


def test_host_selection_prefers_cloud_only_when_available(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setattr(voice_audio, "_installed", lambda module: module == "faster_whisper")
    assert isinstance(build_stt(_Settings()), GroqSpeechToText)

    monkeypatch.delenv("GROQ_API_KEY")
    local = build_stt(_Settings())
    assert isinstance(local, SpeechToText)

    settings = _Settings()
    settings.voice_stt_provider = "local"
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert isinstance(build_stt(settings), SpeechToText)


def test_tts_selection_uses_edge_when_installed(monkeypatch):
    monkeypatch.setattr(voice_audio, "_installed", lambda module: module == "edge_tts")
    assert isinstance(build_tts(_Settings()), EdgeTextToSpeech)

    monkeypatch.setattr(voice_audio, "_installed", lambda module: False)
    assert isinstance(build_tts(_Settings()), voice_audio.TextToSpeech)

    settings = _Settings()
    settings.voice_tts_provider = "piper"
    assert isinstance(build_tts(settings), voice_audio.TextToSpeech)
