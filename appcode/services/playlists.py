"""
M3U playlists, read and written. Pure functions.

Reading accepts what other players write: plain and extended M3U, relative
paths (against the playlist's folder), ``file://`` URLs, Windows line endings
and a UTF-8 BOM. Writing produces extended M3U with absolute paths, which
VLC, mpv and LizardKing all read back.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import unquote, urlsplit


def parse_m3u(text: str, base_dir: str) -> list[dict]:
    """``[{"path" | "url", "title", "duration"}]`` in playlist order."""
    entries: list[dict] = []
    title: Optional[str] = None
    duration: Optional[float] = None
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            head, _, name = line[len("#EXTINF:"):].partition(",")
            try:
                seconds = float(head.split()[0]) if head.split() else -1
            except ValueError:
                seconds = -1
            duration = seconds if seconds >= 0 else None
            title = name.strip() or None
            continue
        if line.startswith("#"):
            continue
        entry: dict = {"title": title, "duration": duration}
        scheme = urlsplit(line).scheme.lower()
        if scheme == "file":
            entry["path"] = unquote(urlsplit(line).path)
        elif len(scheme) > 1:  # "C:" is a drive letter, not a scheme
            entry["url"] = line
        else:
            path = line.replace("\\", "/") if "\\" in line and not os.path.exists(line) else line
            entry["path"] = os.path.normpath(os.path.join(base_dir, os.path.expanduser(path)))
        entries.append(entry)
        title, duration = None, None
    return entries


def write_m3u(entries: list[dict]) -> str:
    """Extended M3U for ``[{"path" | "url", "title"?, "duration"?}]``."""
    lines = ["#EXTM3U"]
    for entry in entries:
        target = entry.get("path") or entry.get("url")
        if not target:
            continue
        duration = entry.get("duration")
        title = (entry.get("title") or "").replace("\n", " ").strip()
        if title or duration:
            lines.append(f"#EXTINF:{int(duration) if duration else -1},{title}")
        lines.append(target)
    return "\n".join(lines) + "\n"
