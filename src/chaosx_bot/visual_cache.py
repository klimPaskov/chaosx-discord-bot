from __future__ import annotations

import hashlib
import json
import os
import shlex
from pathlib import Path
from typing import Any

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
DEFAULT_CACHE_BUDGET_BYTES = 512 * 1024 * 1024


class VisualArtifactCache:
    """Small bounded disk cache for validated Discord-ready MCP PNG artifacts."""

    def __init__(
        self,
        namespace: str,
        *,
        max_item_bytes: int,
        root: Path | None = None,
        budget_bytes: int = DEFAULT_CACHE_BUDGET_BYTES,
    ) -> None:
        cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        base = root or cache_home / "chaosx-discord-bot" / "mcp-v1"
        self.root = base / namespace
        self.max_item_bytes = max_item_bytes
        self.budget_bytes = budget_bytes

    def get(self, key: tuple[Any, ...]) -> bytes | None:
        path = self._path(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data.startswith(PNG_HEADER) or len(data) > self.max_item_bytes:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        try:
            path.touch()
        except OSError:
            pass
        return data

    def put(self, key: tuple[Any, ...], data: bytes) -> None:
        if not data.startswith(PNG_HEADER) or len(data) > self.max_item_bytes:
            return
        path = self._path(key)
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            self._prune()
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _path(self, key: tuple[Any, ...]) -> Path:
        payload = json.dumps(key, ensure_ascii=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.png"

    def _prune(self) -> None:
        try:
            entries = sorted(
                (path for path in self.root.glob("*.png") if path.is_file()),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            return
        kept_bytes = 0
        for path in entries:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            kept_bytes += size
            if kept_bytes <= self.budget_bytes:
                continue
            try:
                path.unlink()
            except OSError:
                pass


def mcp_launcher_fingerprint(command: str, arguments: str) -> tuple[Any, ...]:
    """Invalidate cached renders when the configured local launcher changes."""

    parts = [command, *shlex.split(arguments)]
    files: list[tuple[str, int, int]] = []
    for value in parts:
        path = Path(value).expanduser()
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            files.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
    return command, arguments, tuple(files)
