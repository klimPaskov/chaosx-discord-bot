from __future__ import annotations

import os
from pathlib import Path

from chaosx_bot.visual_cache import PNG_HEADER, VisualArtifactCache


def test_visual_artifact_cache_survives_client_recreation(tmp_path: Path) -> None:
    key = ("events/test.txt", 123, "renderer")
    png = PNG_HEADER + b"payload"

    first = VisualArtifactCache("events", max_item_bytes=1024, root=tmp_path)
    first.put(key, png)
    second = VisualArtifactCache("events", max_item_bytes=1024, root=tmp_path)

    assert second.get(key) == png


def test_visual_artifact_cache_rejects_invalid_or_oversized_data(tmp_path: Path) -> None:
    cache = VisualArtifactCache("events", max_item_bytes=16, root=tmp_path)

    cache.put(("invalid",), b"not-png")
    cache.put(("large",), PNG_HEADER + b"x" * 20)

    assert cache.get(("invalid",)) is None
    assert cache.get(("large",)) is None


def test_visual_artifact_cache_prunes_oldest_entries(tmp_path: Path) -> None:
    cache = VisualArtifactCache(
        "events", max_item_bytes=1024, root=tmp_path, budget_bytes=24
    )
    first = PNG_HEADER + b"a" * 8
    second = PNG_HEADER + b"b" * 8

    cache.put(("first",), first)
    os.utime(cache._path(("first",)), ns=(1, 1))
    cache.put(("second",), second)

    assert cache.get(("first",)) is None
    assert cache.get(("second",)) == second
