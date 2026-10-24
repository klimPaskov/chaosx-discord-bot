"""Breathing must not be speech, and must not interrupt the bot mid-sentence."""
from __future__ import annotations

import struct
import threading

from chaosx_bot.voice_chat import BARGE_IN_MIN_SECONDS, BYTES_PER_SECOND, VoiceReceiver


def _pcm(dbfs: float, seconds: float) -> bytes:
    """16-bit mono PCM at the receiver's frame rate, at a known loudness."""
    frames = max(2, int(BYTES_PER_SECOND * seconds / 2))
    amplitude = int(32768 * (10 ** (dbfs / 20)))
    return struct.pack("<" + "h" * frames, *([amplitude, -amplitude] * (frames // 2)))


def _receiver(*buffers: bytes) -> VoiceReceiver:
    receiver = object.__new__(VoiceReceiver)  # no socket, no handshake: only the buffer logic is under test
    receiver._lock = threading.Lock()
    receiver._buffers = {index: bytearray(buf) for index, buf in enumerate(buffers)}
    receiver._stats = {}
    return receiver


def test_dbfs_matches_the_requested_level() -> None:
    assert abs(VoiceReceiver._dbfs(_pcm(-10.0, 0.4)) - (-10.0)) < 1.0
    assert VoiceReceiver._dbfs(b"") == float("-inf")
    assert VoiceReceiver._dbfs(b"\x00\x00" * 100) == float("-inf")


def test_sustained_speech_barges_in() -> None:
    assert _receiver(_pcm(-12.0, 0.5)).speech_like_incoming() is True


def test_breathing_does_not_barge_in() -> None:
    # a breath or a fan: long enough to fill a buffer, far below speech level
    assert _receiver(_pcm(-52.0, 1.0)).speech_like_incoming() is False


def test_a_single_noisy_frame_does_not_barge_in() -> None:
    assert _receiver(_pcm(-8.0, 0.05)).speech_like_incoming() is False


def test_barge_in_ignores_a_quiet_buffer_beside_a_loud_one() -> None:
    receiver = _receiver(_pcm(-55.0, 1.0), _pcm(-12.0, 0.4))
    assert receiver.speech_like_incoming() is True, "one real speaker is enough"


def test_no_buffers_means_no_barge_in() -> None:
    assert _receiver().speech_like_incoming() is False
    assert BARGE_IN_MIN_SECONDS >= 0.2, "an interruption must be sustained, not instantaneous"
