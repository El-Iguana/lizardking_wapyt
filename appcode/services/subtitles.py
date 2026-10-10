"""
Subtitles as WebVTT, the only format a ``<track>`` takes.

Two sources: sidecar files next to the video (``Film.srt``, ``Film.en.srt``,
``Film.English.ass``) and text streams inside it (SubRip, ASS, mov_text).
Both go through ``ffmpeg -f webvtt``. Image subtitles (PGS, VobSub) cannot
become text; burning them in is on the roadmap.

``sidecars`` is pure apart from listing one directory. Conversions are cached
in memory by source path, mtime and stream.
"""
from __future__ import annotations

import os
import subprocess
import threading
from typing import Optional

from services import media
from services.config import SUBTITLE_EXT, ffmpeg_bin

# Two- and three-letter codes, and a few names people actually put in file names.
LANGUAGE_NAMES = {
    "en": "English", "eng": "English", "english": "English",
    "es": "Spanish", "spa": "Spanish", "spanish": "Spanish",
    "fr": "French", "fre": "French", "fra": "French", "french": "French",
    "de": "German", "ger": "German", "deu": "German", "german": "German",
    "it": "Italian", "ita": "Italian", "italian": "Italian",
    "pt": "Portuguese", "por": "Portuguese", "portuguese": "Portuguese",
    "nl": "Dutch", "dut": "Dutch", "nld": "Dutch", "dutch": "Dutch",
    "ja": "Japanese", "jpn": "Japanese", "japanese": "Japanese",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese", "chinese": "Chinese",
    "ko": "Korean", "kor": "Korean", "korean": "Korean",
    "ru": "Russian", "rus": "Russian", "russian": "Russian",
    "sv": "Swedish", "swe": "Swedish", "pl": "Polish", "pol": "Polish",
}
CONVERTIBLE = SUBTITLE_EXT - {".sub"}  # a .sub is usually VobSub images


def language_label(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return LANGUAGE_NAMES.get(code.lower(), code)


def sidecars(video_path: str) -> list[dict]:
    """Subtitle files beside a video whose name starts with the video's: ``[{path, label, language}]``."""
    folder, name = os.path.split(video_path)
    stem = os.path.splitext(name)[0]
    try:
        names = sorted(os.listdir(folder or "."))
    except OSError:
        return []
    found = []
    for other in names:
        base, ext = os.path.splitext(other)
        if ext.lower() not in CONVERTIBLE or not base.lower().startswith(stem.lower()):
            continue
        rest = base[len(stem):].strip(" ._-")
        tags = [t for t in rest.replace("_", ".").replace("-", ".").split(".") if t]
        language = next((t.lower() for t in tags if t.lower() in LANGUAGE_NAMES), None)
        label = language_label(language) or (rest or "Subtitles")
        if any(t.lower() in ("forced", "sdh", "cc") for t in tags):
            label += " (" + ", ".join(t for t in tags if t.lower() in ("forced", "sdh", "cc")) + ")"
        found.append({"path": os.path.join(folder, other), "label": label, "language": language})
    return found


_cache: dict[tuple[str, int, Optional[int]], bytes] = {}
_lock = threading.Lock()


class SubtitleError(Exception):
    pass


def to_vtt(path: str, stream: Optional[int] = None) -> bytes:
    """A subtitle file, or text stream ``stream`` (ffprobe index) of a media file, as WebVTT."""
    key = (path, 0 if media.is_remote(path) else os.stat(path).st_mtime_ns, stream)
    with _lock:
        if key in _cache:
            return _cache[key]
    args = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", *media.input_args(path), "-i", path]
    if stream is not None:
        args += ["-map", f"0:{int(stream)}"]
    args += ["-f", "webvtt", "-"]
    try:
        out = subprocess.run(args, capture_output=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as error:
        raise SubtitleError("converting the subtitles took too long") from error
    if out.returncode != 0 or not out.stdout.startswith(b"WEBVTT"):
        lines = out.stderr.decode(errors="replace").strip().splitlines()
        raise SubtitleError(lines[-1] if lines else "ffmpeg could not convert these subtitles")
    with _lock:
        if len(_cache) > 200:
            _cache.clear()
        _cache[key] = out.stdout
    return out.stdout
