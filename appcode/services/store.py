"""
What LizardKing remembers between runs: recent files, where you stopped in
each, and settings. One JSON file, ``state.json`` in the data directory,
replaced atomically on every change.

Plain module: one lock and one in-memory copy for the process.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from services.config import data_dir

RECENT_LIMIT = 30
POSITIONS_LIMIT = 1000
# Resume only from a real position, and not from the credits.
RESUME_MIN_SECONDS = 15.0
RESUME_TAIL_SECONDS = 20.0
RESUME_TAIL_FRACTION = 0.97

DEFAULT_SETTINGS: dict[str, Any] = {
    "volume": 1.0,
    "muted": False,
    "theme": "dark",
    "resume": True,
    "playlist_open": True,
    "last_dir": "",
    "repeat": "off",
    "shuffle": False,
}

_lock = threading.Lock()
_state: Optional[dict] = None
_path_override: Optional[Path] = None


def use_file(path: Optional[Path]) -> None:
    """Point the store somewhere else (tests); None goes back to the data directory."""
    global _state, _path_override
    with _lock:
        _state, _path_override = None, path


def _file() -> Path:
    return _path_override or (data_dir() / "state.json")


def _load() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _state = {}
        _state.setdefault("recent", [])
        _state.setdefault("positions", {})
        _state["settings"] = {**DEFAULT_SETTINGS, **(_state.get("settings") or {})}
    return _state


def _save() -> None:
    target = _file()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def add_recent(path: str, title: str, kind: str) -> None:
    with _lock:
        state = _load()
        state["recent"] = [r for r in state["recent"] if r.get("path") != path]
        state["recent"].insert(0, {"path": path, "title": title, "kind": kind, "at": time.time()})
        del state["recent"][RECENT_LIMIT:]
        _save()


def recent() -> list[dict]:
    with _lock:
        return [dict(r) for r in _load()["recent"]]


def clear_recent() -> None:
    with _lock:
        _load()["recent"] = []
        _save()


def is_resumable(position: float, duration: Optional[float]) -> bool:
    if position < RESUME_MIN_SECONDS:
        return False
    if duration and (position > duration - RESUME_TAIL_SECONDS or position > duration * RESUME_TAIL_FRACTION):
        return False
    return True


def set_position(path: str, position: float, duration: Optional[float]) -> None:
    """Remember where playback is; forget it near the start or the end."""
    with _lock:
        positions = _load()["positions"]
        if is_resumable(position, duration):
            positions.pop(path, None)  # re-insert so the newest is last
            positions[path] = {"position": round(position, 1), "duration": duration, "at": time.time()}
            while len(positions) > POSITIONS_LIMIT:
                positions.pop(next(iter(positions)))
        elif path in positions:
            del positions[path]
        else:
            return
        _save()


def position(path: str) -> Optional[float]:
    with _lock:
        entry = _load()["positions"].get(path)
        return entry["position"] if entry else None


def settings() -> dict:
    with _lock:
        return dict(_load()["settings"])


def update_settings(values: dict) -> dict:
    with _lock:
        current = _load()["settings"]
        for key, value in values.items():
            if key in DEFAULT_SETTINGS:
                current[key] = value
        _save()
        return dict(current)
