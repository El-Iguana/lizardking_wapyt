"""
Video thumbnails for the Open dialog: one frame, ffmpeg, cached on disk.

A frame from about a tenth of the way in (past titles and black), scaled to
THUMB_WIDTH, as JPEG. Made on demand when the dialog's lazy ``<img>`` asks,
at most MAX_JOBS at once so a folder of films does not flood a NAS, and kept
under ``cache_dir()/thumbs`` (the transcode cache is wiped at start; this one
is not), keyed by the address and what the server said about the file, so a
rescanned server that reuses an address does not show the old picture.

Plain module: the semaphore and the memory cache live for the process.
"""
from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path
from typing import Optional

from services import media
from services.config import cache_dir, ffmpeg_bin

THUMB_WIDTH = 320
MAX_JOBS = 2
MAX_FILES = 3000          # on disk; the oldest go first
SEEK_FRACTION = 0.1
SEEK_MAX = 300.0          # seconds: a long film's tenth is far into a slow file
SEEK_UNKNOWN = 30.0

_jobs = threading.BoundedSemaphore(MAX_JOBS)
_memory: dict[str, bytes] = {}
_lock = threading.Lock()


class ThumbError(Exception):
    pass


def _dir() -> Path:
    path = cache_dir() / "thumbs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def key(path: str, size: Optional[int] = None, duration: Optional[float] = None) -> str:
    return hashlib.sha256(f"{path}|{size or ''}|{round(duration or 0, 1)}".encode()).hexdigest()[:32]


def seek_for(duration: Optional[float]) -> float:
    """Where to take the frame: a tenth of the way in, capped; early for a short clip."""
    if not duration or duration <= 0:
        return SEEK_UNKNOWN
    return min(duration * SEEK_FRACTION, SEEK_MAX)


def ffmpeg_args(path: str, at: float) -> list[str]:
    """One frame at ``at`` seconds, THUMB_WIDTH wide, as JPEG on stdout. Pure."""
    return [ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", "-ss", f"{at:.3f}",
            *media.input_args(path), "-i", path, "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"scale={THUMB_WIDTH}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "-"]


def thumbnail(path: str, size: Optional[int] = None, duration: Optional[float] = None) -> bytes:
    """A JPEG frame of ``path`` (a file or a media server's URL), from the cache when it can."""
    name = key(path, size, duration)
    with _lock:
        if name in _memory:
            return _memory[name]
    target = _dir() / f"{name}.jpg"
    if target.is_file():
        body = target.read_bytes()
    else:
        with _jobs:
            if target.is_file():           # made by another request while this one waited
                body = target.read_bytes()
            else:
                body = _make(path, seek_for(duration))
                if not body and duration:  # shorter than it says, or an odd seek: the start
                    body = _make(path, 0.0)
                if not body:
                    raise ThumbError("ffmpeg could not take a frame")
                tmp = target.with_suffix(".tmp")
                tmp.write_bytes(body)
                tmp.replace(target)
                _prune()
    with _lock:
        if len(_memory) > 300:
            _memory.clear()
        _memory[name] = body
    return body


def _make(path: str, at: float) -> bytes:
    try:
        out = subprocess.run(ffmpeg_args(path, at), capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return out.stdout if out.returncode == 0 and out.stdout.startswith(b"\xff\xd8") else b""


def _prune() -> None:
    files = sorted(_dir().glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    for old in files[:max(0, len(files) - MAX_FILES)]:
        old.unlink(missing_ok=True)
