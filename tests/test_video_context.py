"""Video pipeline tests: detection, sampling, and prompt-block formatting.

The ffmpeg/whisper stages are exercised end-to-end by hand against a generated
clip; these tests cover the pure decision/formatting logic that routes a file
into that pipeline and tells the model what it received.
"""

from chaosx_bot.video_context import (
    format_duration,
    format_video_block,
    frame_timestamps,
    is_video_link,
    looks_like_video,
)

# Minimal container headers (magic bytes only — enough for detection).
MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 32
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  " + b"\x00" * 32
WEBM = b"\x1aE\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x1fB\x86\x81\x01B\xf7\x81\x01B\xf2\x81\x04"
AVI = b"RIFF\x24\x08\x00\x00AVI LIST" + b"\x00" * 32
MPEG_PS = b"\x00\x00\x01\xba\x21\x00\x01\x00\x01\x80" + b"\x00" * 32
FLV = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
TEXT = b"Chaos Redux Event 006 notes\n" * 4


def test_detects_video_containers_by_magic_bytes():
    assert looks_like_video(MP4)
    assert looks_like_video(MOV)
    assert looks_like_video(WEBM)
    assert looks_like_video(AVI)
    assert looks_like_video(MPEG_PS)
    assert looks_like_video(FLV)


def test_ignores_non_video_payloads():
    assert not looks_like_video(PNG)
    assert not looks_like_video(TEXT)
    assert not looks_like_video(b"")
    # Extension alone must not pull a mislabelled file into the video pipeline.
    assert not looks_like_video(TEXT, ext=".mp4")
    assert looks_like_video(TEXT, ext=".mp4", content_type="video/mp4")
    assert not looks_like_video(TEXT, ext=".mp4", content_type="text/plain")


def test_video_link_detection():
    assert is_video_link("https://www.youtube.com/watch?v=abc")
    assert is_video_link("https://youtu.be/abc")
    assert is_video_link("https://clips.twitch.tv/SomeClip")
    assert is_video_link("https://cdn.example.com/clip.mp4")
    assert is_video_link("https://example.com/path/movie.WEBM")
    assert not is_video_link("https://example.com/article")
    assert not is_video_link("https://notyoutube.com.evil.example/watch")
    assert not is_video_link("not a url")


def test_frame_timestamps_are_evenly_spaced_inside_the_clip():
    stamps = frame_timestamps(100.0, 4)
    assert stamps == [12.5, 37.5, 62.5, 87.5]
    assert all(0 < value < 100 for value in stamps)
    assert frame_timestamps(0.0, 3) == [0.0, 0.0, 0.0]
    assert frame_timestamps(10.0, 0) == []


def test_format_duration():
    assert format_duration(0) == "0:00"
    assert format_duration(59.6) == "1:00"
    assert format_duration(75) == "1:15"
    assert format_duration(3725) == "1:02:05"


def test_video_block_states_what_was_extracted():
    block = format_video_block(
        label="clip.mp4",
        probe={"duration": 90.0, "width": 1280, "height": 720, "has_audio": True, "has_video": True},
        transcript="Chaos Redux zombie outbreak is being tested here.",
        frames=2,
        timestamps=[22.5, 67.5],
    )
    assert block.startswith("## Attached video: clip.mp4 (length 1:30, 1280x720, audio: yes)")
    assert "2 frame(s) sampled at 0:22, 1:08" in block
    assert "Speech transcript (automatic, may contain errors):" in block
    assert "zombie outbreak is being tested here." in block


def test_video_block_reports_missing_transcript_honestly():
    silent = format_video_block(
        label="clip.mp4",
        probe={"duration": 10, "has_audio": False, "has_video": True},
        transcript_note="none (the file has no audio track)",
    )
    assert "audio: none" in silent
    assert "none (the file has no audio track)" in silent
    skipped = format_video_block(
        label="clip.mp4",
        probe={"duration": 10, "has_audio": True, "has_video": True},
        transcript_note="skipped (no local speech model installed on the bot host)",
    )
    assert "skipped (no local speech model installed" in skipped
    assert '"""' not in skipped


def test_video_block_notes_truncation():
    block = format_video_block(
        label="long.mp4",
        probe={"duration": 3600, "has_audio": True, "has_video": True, "processed_seconds": 300},
        transcript="...",
        frames=1,
        timestamps=[150.0],
        truncated=True,
    )
    assert "only the first 5:00 were analysed" in block
