"""
Plain HTTP routes for the media itself: files, transcode sessions,
subtitles and cover art. ``<video>`` and hls.js fetch these directly, so they
are ordinary GETs rather than BFF calls.

Every route takes an opaque token or session id (see ``tokens``), never a
path. The handlers are sync ``def``s so Starlette runs them in its thread pool:
a segment request can wait on the encoder for several seconds.
"""
from __future__ import annotations

import subprocess
import threading

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse

from services import subtitles, tokens, transcode
from services.config import APP_NAME, VERSION, ffmpeg_bin

router = APIRouter()

_covers: dict[str, bytes] = {}
_covers_lock = threading.Lock()


def _path(token: str) -> str:
    path = tokens.path_for(token)
    if not path:
        raise HTTPException(status_code=404, detail="Unknown media")
    return path


@router.get("/lk/health")
def health() -> dict:
    return {"app": APP_NAME, "version": VERSION, "encoders": transcode.sessions.running_count()}


@router.get("/lk/file/{token}")
def media_file(token: str) -> FileResponse:
    """The file as it is. Starlette answers Range requests, which seeking needs."""
    path = _path(token)
    try:
        return FileResponse(path, headers={"Cache-Control": "no-store"})
    except OSError as error:
        raise HTTPException(status_code=404, detail="File is gone") from error


@router.get("/lk/hls/{sid}/index.m3u8")
def hls_playlist(sid: str) -> Response:
    session = transcode.sessions.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="No such session")
    return Response(transcode.vod_playlist(session.plan), media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


@router.get("/lk/hls/{sid}/{name}")
def hls_segment(sid: str, name: str) -> FileResponse:
    session = transcode.sessions.get(sid)
    if not session:
        raise HTTPException(status_code=404, detail="No such session")
    try:
        path = transcode.sessions.segment(session, name)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail="No such segment") from error
    except transcode.TranscodeError as error:
        raise HTTPException(status_code=500, detail=f"Transcode failed: {error}") from error
    media_type = "video/mp4" if name.endswith(".mp4") else "video/iso.segment"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.get("/lk/vtt/{token}")
def vtt(token: str, stream: int | None = None) -> PlainTextResponse:
    path = _path(token)
    try:
        body = subtitles.to_vtt(path, stream)
    except (OSError, subtitles.SubtitleError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return PlainTextResponse(body, media_type="text/vtt; charset=utf-8")


@router.get("/lk/cover/{token}")
def cover(token: str) -> Response:
    """Cover art embedded in an audio file, as JPEG."""
    path = _path(token)
    with _covers_lock:
        body = _covers.get(path)
    if body is None:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", "-i", path, "-map", "0:v:0", "-frames:v", "1",
             "-vf", "scale='min(600,iw)':-2", "-f", "image2", "-c:v", "mjpeg", "-"],
            capture_output=True, timeout=30, check=False)
        if out.returncode != 0 or not out.stdout:
            raise HTTPException(status_code=404, detail="No cover art")
        body = out.stdout
        with _covers_lock:
            if len(_covers) > 300:
                _covers.clear()
            _covers[path] = body
    return Response(body, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
