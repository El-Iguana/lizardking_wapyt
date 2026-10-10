"""
Opaque URLs for local files.

The page never puts a filesystem path in a media URL. A path is registered
through a BFF call (which pytincture pins to this origin) and comes back as a
random token; ``/lk/file/<token>`` serves that file and nothing else. Another
web page cannot guess a token, so it cannot point a ``<video>`` at your files.

Plain module: the table lives for the life of the process.
"""
from __future__ import annotations

import secrets
import threading
from typing import Optional

_by_token: dict[str, str] = {}
_by_path: dict[str, str] = {}
_lock = threading.Lock()


def token_for(path: str) -> str:
    with _lock:
        token = _by_path.get(path)
        if token is None:
            token = secrets.token_urlsafe(16)
            _by_token[token], _by_path[path] = path, token
        return token


def path_for(token: str) -> Optional[str]:
    with _lock:
        return _by_token.get(token)
