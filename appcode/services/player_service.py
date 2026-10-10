"""
The page's server API: browse the disk, turn a file into something the
browser can play, remember positions and recent files, read and write
playlists.

Every export is a sync ``def``: pytincture runs it in a worker thread, so
ffprobe and file I/O are fine here, and the browser calls ``*_async``.

**Annotations are enforced.** pytincture checks each JSON argument against
the parameter's annotation before the call, and answers 400 on a mismatch:
``audio: int = None`` refuses the ``null`` the page sends for "no choice".
Anything that may be None is written ``int | None``.

**No state at module level.** pytincture re-executes this file on every call,
so a dict defined here would be new and empty each time. Everything that
persists lives in the plain modules this imports (``tokens``, ``transcode``,
``store``, ``startup``).
"""
from __future__ import annotations

import os

from pytincture.dataclass import backend_for_frontend

from services import browse, media, playlists, startup, store, subtitles, tokens, transcode
from services.config import APP_NAME, REPO_URL, VERSION


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _title(path: str, info: dict) -> tuple[str, str]:
    """Title and subtitle: tags when the file has them, else the file name."""
    tags = info.get("tags") or {}
    stem = os.path.splitext(os.path.basename(path))[0]
    if media.kind(info) == "audio":
        artist = tags.get("artist") or tags.get("album_artist") or ""
        album = tags.get("album") or ""
        return tags.get("title") or stem, " — ".join(x for x in (artist, album) if x)
    return tags.get("title") or stem, ""


def _audio_label(n: int, a: dict) -> str:
    parts = [a.get("title") or subtitles.language_label(a.get("language")) or f"Track {n + 1}"]
    detail = (a.get("codec") or "").upper()
    if a.get("channels"):
        detail += {1: " mono", 2: " stereo", 6: " 5.1", 8: " 7.1"}.get(a["channels"], f" {a['channels']}ch")
    return f"{parts[0]} ({detail.strip()})" if detail.strip() else parts[0]


@backend_for_frontend
class PlayerService:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    # Disk ----------------------------------------------------------------

    def places(self) -> list:
        return browse.places()

    def browse(self, path: str = "", show_all: bool = False) -> dict:
        if not path:
            path = store.settings().get("last_dir") or ""
        result = browse.listing(path, show_all=show_all)
        if not result["error"]:
            store.update_settings({"last_dir": result["path"]})
        return result

    def expand(self, paths: list, recursive: bool = True) -> list:
        """Picked files, folders and playlists as queue entries."""
        return browse.expand([_expand(p) for p in paths], recursive=recursive)

    def startup(self) -> list:
        """Files named on the command line, once."""
        return browse.expand(startup.take())

    # Playing -------------------------------------------------------------

    def prepare(self, path: str, caps: dict | None = None, audio: int | None = None, force_transcode: bool = False) -> dict:
        """
        Everything the player needs for one file: its URL (the file itself, or
        a transcode session), subtitles, title, where to resume, and why it is
        played the way it is.
        """
        path = _expand(path)
        if not os.path.isfile(path):
            raise ValueError(f"{os.path.basename(path)} is not there any more.")
        try:
            info = media.probe(path)
        except media.ProbeError as error:
            raise ValueError(f"Cannot read {os.path.basename(path)}: {error}") from error
        ext = os.path.splitext(path)[1]
        decision = media.plan(info, ext, caps or {}, audio)
        if force_transcode and decision["mode"] == "direct" and info.get("duration"):
            decision = {"mode": "transcode", "reasons": ["the browser could not decode it as it is"]}
        if decision["mode"] == "none":
            raise ValueError(f"Cannot play {os.path.basename(path)}: {'; '.join(decision['reasons'])}.")

        token = tokens.token_for(path)
        if decision["mode"] == "direct":
            src = f"/lk/file/{token}"
        else:
            session = transcode.sessions.open(path, info, audio)
            src = f"/lk/hls/{session.id}/index.m3u8"

        tracks = []
        for side in subtitles.sidecars(path):
            tracks.append({"src": f"/lk/vtt/{tokens.token_for(side['path'])}", "label": side["label"],
                           "language": side["language"]})
        for s in info.get("subtitles", []):
            if s.get("text"):
                label = s.get("title") or subtitles.language_label(s.get("language")) or f"Track {s['index']}"
                if s.get("forced"):
                    label += " (forced)"
                tracks.append({"src": f"/lk/vtt/{token}?stream={s['index']}", "label": label,
                               "language": s.get("language"), "default": bool(s.get("forced"))})

        title, subtitle = _title(path, info)
        kind = media.kind(info)
        cover = next((v for v in info.get("video", []) if v.get("attached_pic")), None)
        settings = store.settings()
        start = store.position(path) if settings.get("resume") else None
        store.add_recent(path, title, kind)
        chosen = media.default_audio(info) if audio is None else audio
        return {
            "src": src, "title": title, "subtitle": subtitle, "kind": kind,
            "poster": f"/lk/cover/{token}" if cover else None,
            "start": start or 0, "text_tracks": tracks,
            "mode": decision["mode"], "reasons": decision["reasons"],
            "audio_tracks": [{"index": n, "label": _audio_label(n, a)} for n, a in enumerate(info.get("audio", []))],
            "audio": chosen, "duration": info.get("duration"),
            "fps": (media.picture(info) or {}).get("fps") or 0,
            "skipped_subtitles": sum(1 for s in info.get("subtitles", []) if not s.get("text")),
        }

    def info(self, path: str, caps: dict | None = None, audio: int | None = None) -> dict:
        """The Media Information dialog: streams, tags and how it plays."""
        path = _expand(path)
        try:
            info = media.probe(path)
        except media.ProbeError as error:
            raise ValueError(str(error)) from error
        decision = media.plan(info, os.path.splitext(path)[1], caps or {}, audio)
        return {"path": path, "name": os.path.basename(path), "info": info, "plan": decision,
                "encoder": transcode.encoder() if decision["mode"] == "transcode" else None}

    def save_position(self, path: str, position: float, duration: float | None = None) -> None:
        store.set_position(_expand(path), float(position or 0), duration)

    # Recent, settings ----------------------------------------------------

    def recent(self) -> list:
        return [r for r in store.recent() if os.path.exists(r["path"])]

    def clear_recent(self) -> None:
        store.clear_recent()

    def settings(self) -> dict:
        return store.settings()

    def update_settings(self, values: dict) -> dict:
        return store.update_settings(values or {})

    # Playlists -----------------------------------------------------------

    def save_playlist(self, path: str, entries: list) -> dict:
        target = _expand(path)
        if os.path.splitext(target)[1].lower() not in (".m3u", ".m3u8"):
            target += ".m3u8"
        if not os.path.isdir(os.path.dirname(target)):
            raise ValueError(f"There is no folder {os.path.dirname(target)}.")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(playlists.write_m3u(entries))
        return {"path": target, "count": len(entries)}

    def about(self) -> dict:
        return {"name": APP_NAME, "version": VERSION, "repo": REPO_URL, "encoder": transcode.encoder()}
