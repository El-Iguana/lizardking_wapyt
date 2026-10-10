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
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse

from services import dlna, media, subtitles, tokens, transcode
from services.config import APP_NAME, VERSION, ffmpeg_bin

router = APIRouter()

_covers: dict[str, bytes] = {}
_art: dict[str, tuple[bytes, str]] = {}
ART_MAX_BYTES = 8 * 1024 * 1024
ART_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
_covers_lock = threading.Lock()


def _path(token: str) -> str:
    path = tokens.path_for(token)
    if not path:
        raise HTTPException(status_code=404, detail="Unknown media")
    return path


@router.get("/lk/health")
def health() -> dict:
    return {"app": APP_NAME, "version": VERSION, "encoders": transcode.sessions.running_count()}


PROXY_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified")
PROXY_CHUNK = 256 * 1024


def _proxy(url: str, range_header: str | None) -> Response:
    """
    A media server's file, passed through with its Range answers. Same-origin
    for the page, so seeking works and a snapshot's canvas is not tainted.
    """
    try:
        dlna.check_url(url)
    except dlna.DlnaError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    request = urllib.request.Request(url, headers={"Range": range_header} if range_header else {})
    try:
        upstream = urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as error:
        raise HTTPException(status_code=error.code, detail="The media server refused the request") from error
    except OSError as error:
        raise HTTPException(status_code=502, detail="The media server did not answer") from error
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in PROXY_HEADERS}
    headers["Cache-Control"] = "no-store"

    def body():
        try:
            while chunk := upstream.read(PROXY_CHUNK):
                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(body(), status_code=upstream.status, headers=headers)


@router.get("/lk/file/{token}")
def media_file(token: str, request: Request) -> Response:
    """The file as it is. Starlette answers Range requests, which seeking needs."""
    path = _path(token)
    if media.is_remote(path):
        return _proxy(path, request.headers.get("range"))
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
            [ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error", *media.input_args(path), "-i", path,
             "-map", "0:v:0", "-frames:v", "1",
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


@router.get("/lk/art/{token}")
def art(token: str) -> Response:
    """A media server's album art, fetched for the page (which may only load images from here)."""
    url = _path(token)
    try:
        dlna.check_url(url)
    except dlna.DlnaError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    with _covers_lock:
        cached = _art.get(url)
    if cached is None:
        try:
            with urllib.request.urlopen(url, timeout=10) as upstream:
                kind = upstream.headers.get_content_type()
                body = upstream.read(ART_MAX_BYTES + 1)
        except (urllib.error.URLError, OSError) as error:
            raise HTTPException(status_code=404, detail="No album art") from error
        # Raster images only: an SVG served from this origin could carry script.
        if kind not in ART_TYPES or len(body) > ART_MAX_BYTES:
            raise HTTPException(status_code=404, detail="No album art")
        cached = (body, kind)
        with _covers_lock:
            if len(_art) > 500:
                _art.clear()
            _art[url] = cached
    return Response(cached[0], media_type=cached[1], headers={"Cache-Control": "private, max-age=3600"})
