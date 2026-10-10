"""
Where LizardKing keeps things, and what it treats as media.

Plain module, server side only. State that has to outlive a request lives in
plain modules like this one: pytincture re-executes a BFF module on every call,
so anything defined at module level there is new and empty each time.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "LizardKing"
VERSION = "0.1.0"
REPO_URL = "https://github.com/El-Iguana/lizardking_wapyt"

VIDEO_EXT = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg",
    ".m2ts", ".mts", ".ts", ".vob", ".ogv", ".3gp", ".3g2", ".divx", ".asf", ".rm", ".rmvb",
})
AUDIO_EXT = frozenset({
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav", ".wma",
    ".ape", ".wv", ".alac", ".aiff", ".aif", ".mka", ".ac3", ".dts", ".mpc", ".tta",
})
SUBTITLE_EXT = frozenset({".srt", ".vtt", ".ass", ".ssa", ".sub"})
PLAYLIST_EXT = frozenset({".m3u", ".m3u8"})


def kind_of(name: str) -> str | None:
    """``video``, ``audio``, ``subtitle``, ``playlist`` or None, from the name alone."""
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in SUBTITLE_EXT:
        return "subtitle"
    if ext in PLAYLIST_EXT:
        return "playlist"
    return None


def _xdg(variable: str, fallback: str) -> Path:
    value = os.getenv(variable)
    return Path(value) if value else Path.home() / fallback


def data_dir() -> Path:
    """Recent files, resume positions, settings."""
    override = os.getenv("LIZARDKING_DATA")
    path = Path(override) if override else _xdg("XDG_DATA_HOME", ".local/share") / "lizardking"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """Transcode sessions and converted subtitles; emptied at every start."""
    override = os.getenv("LIZARDKING_CACHE")
    path = Path(override) if override else _xdg("XDG_CACHE_HOME", ".cache") / "lizardking"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ffmpeg_bin() -> str:
    return os.getenv("LIZARDKING_FFMPEG", "ffmpeg")


def ffprobe_bin() -> str:
    return os.getenv("LIZARDKING_FFPROBE", "ffprobe")
