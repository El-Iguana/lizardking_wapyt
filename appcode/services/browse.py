"""
The Open dialog's view of the disk: folders, the media in them, and places to
start from. Plain module (server side).
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Optional

from services import playlists
from services.config import kind_of

# XDG user directories, by the names their folders usually have.
_PLACES = (("Home", ""), ("Videos", "Videos"), ("Music", "Music"), ("Downloads", "Downloads"), ("Desktop", "Desktop"))
MAX_EXPAND = 5000  # files from one "add folder", so a mistaken / does not hang the page


def places() -> list[dict]:
    """Home, the usual user folders that exist, and mounted drives."""
    home = Path.home()
    found = []
    for label, sub in _PLACES:
        path = home / sub if sub else home
        if path.is_dir():
            found.append({"label": label, "path": str(path), "icon": "mdi-home" if not sub else "mdi-folder"})
    user = getpass.getuser()
    # The same drive is often reachable twice: on Fedora Atomic /mnt links to
    # /var/mnt and /media to /run/media. One button per real directory.
    seen = {os.path.realpath(p["path"]) for p in found}
    for parent in (Path("/run/media") / user, Path("/media") / user, Path("/var/mnt"), Path("/mnt")):
        try:
            mounts = sorted(p for p in parent.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            continue
        for mount in mounts:
            real = os.path.realpath(mount)
            if real in seen:
                continue
            seen.add(real)
            found.append({"label": mount.name, "path": str(mount), "icon": "mdi-harddisk"})
    return found


def resolve_dir(path: str) -> str:
    """An existing directory for ``path``: itself, its nearest existing parent, or home."""
    candidate = Path(os.path.expanduser(path or "~")).resolve() if path else Path.home()
    while not candidate.is_dir():
        if candidate.parent == candidate:
            return str(Path.home())
        candidate = candidate.parent
    return str(candidate)


def listing(path: str, show_all: bool = False, show_hidden: bool = False) -> dict:
    """``{path, parent, entries: [{name, path, kind, size, is_dir}]}``, folders first."""
    folder = resolve_dir(path)
    entries = []
    try:
        with os.scandir(folder) as it:
            for entry in it:
                if not show_hidden and entry.name.startswith("."):
                    continue
                try:
                    is_dir = entry.is_dir()
                    size = None if is_dir else entry.stat().st_size
                except OSError:
                    continue
                kind = "folder" if is_dir else kind_of(entry.name)
                if not is_dir and kind is None and not show_all:
                    continue
                entries.append({"name": entry.name, "path": entry.path, "kind": kind or "file",
                                "size": size, "is_dir": is_dir})
        error = None
    except PermissionError:
        error = "You do not have permission to open this folder."
    except OSError as exc:
        error = exc.strerror or str(exc)
    entries.sort(key=lambda e: (not e["is_dir"], _natural(e["name"])))
    parent = str(Path(folder).parent) if Path(folder).parent != Path(folder) else None
    return {"path": folder, "parent": parent, "entries": entries, "error": error}


def _natural(name: str) -> list:
    """Sort key where "Episode 2" comes before "Episode 10"."""
    parts, digits, current = [], False, ""
    for ch in name.lower():
        if ch.isdigit() != digits and current:
            parts.append((0, int(current), "") if digits else (1, 0, current))
            current = ""
        digits = ch.isdigit()
        current += ch
    if current:
        parts.append((0, int(current), "") if digits else (1, 0, current))
    return parts


def media_in(folder: str, recursive: bool) -> list[str]:
    """Playable files in a folder (and below), in natural order, capped at MAX_EXPAND."""
    found: list[str] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted((d for d in dirs if not d.startswith(".")), key=_natural) if recursive else []
        for name in sorted(files, key=_natural):
            if kind_of(name) in ("video", "audio"):
                found.append(os.path.join(root, name))
                if len(found) >= MAX_EXPAND:
                    return found
    return found


def expand(paths: list[str], recursive: bool = True) -> list[dict]:
    """
    Turn what was picked into queue entries ``[{path | url, title, kind}]``:
    folders become the media in them, playlists their entries, other files
    themselves. Subtitle files and anything unplayable are skipped.
    """
    out: list[dict] = []
    for raw in paths:
        path = os.path.expanduser(raw)
        if os.path.isdir(path):
            out += [_entry(p) for p in media_in(path, recursive)]
            continue
        kind = kind_of(path)
        if kind == "playlist":
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for item in playlists.parse_m3u(text, os.path.dirname(path)):
                if item.get("url"):
                    out.append({"url": item["url"], "title": item.get("title") or item["url"], "kind": "video"})
                elif os.path.isfile(item["path"]) and kind_of(item["path"]) in ("video", "audio"):
                    out.append(_entry(item["path"], item.get("title")))
        elif kind in ("video", "audio") and os.path.isfile(path):
            out.append(_entry(path))
    return out


def _entry(path: str, title: Optional[str] = None) -> dict:
    return {"path": path, "title": title or os.path.splitext(os.path.basename(path))[0], "kind": kind_of(path)}
