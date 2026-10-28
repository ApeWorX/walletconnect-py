"""Simple JSON-file key-value store (one file per key under a root directory)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def default_storage_path() -> Path:
    """Return ``~/…/walletconnect-py`` via platformdirs (or a local fallback)."""
    try:
        from platformdirs import user_data_dir

        return Path(user_data_dir("walletconnect-py", "ApeWorX"))
    except ImportError:  # pragma: no cover
        return Path.home() / ".local" / "share" / "walletconnect-py"


class KeyValueStorage:
    """Atomic JSON file store. Keys are sanitized path segments."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_storage_path()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("..", "_")
        return self.root / f"{safe}.json"

    def get(self, key: str, default: Any = None) -> Any:
        path = self._path(key)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def keys(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if not p.name.startswith("."))
