"""
Files named on the command line (``python service.py film.mkv album/``),
handed to the first page that asks for them. Plain module.
"""
from __future__ import annotations

import threading

_pending: list[str] = []
_lock = threading.Lock()


def queue(paths: list[str]) -> None:
    with _lock:
        _pending.extend(paths)


def take() -> list[str]:
    with _lock:
        taken = list(_pending)
        _pending.clear()
        return taken
