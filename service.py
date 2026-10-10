#!/usr/bin/env python3
"""
LizardKing: a local media player in the browser.

    uv run python service.py [FILE_OR_FOLDER ...] [--no-browser]

Starts the server on 127.0.0.1, prints the link and opens it. Files and
folders named on the command line are queued when the page loads.

Everything server-side is wired here, so ``appcode/lizardking.py`` stays pure
browser code (pytincture serves that file's source to Pyodide).
"""
from __future__ import annotations

import argparse
import hmac
import mimetypes
import os
import secrets
import sys
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

HERE = Path(__file__).resolve().parent
APPCODE = HERE / "appcode"
# The BFF and route modules import each other as `services.<name>`, and
# pytincture loads them from the modules folder, so it has to be importable.
sys.path.insert(0, str(APPCODE))

for suffix, kind in ((".js", "text/javascript"), (".css", "text/css"), (".m3u8", "application/vnd.apple.mpegurl"),
                     (".m4s", "video/iso.segment"), (".vtt", "text/vtt"), (".mkv", "video/x-matroska"),
                     (".webm", "video/webm"), (".m4a", "audio/mp4"), (".opus", "audio/ogg"), (".flac", "audio/flac")):
    mimetypes.add_type(kind, suffix)

from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pytincture import PytinctureConfig, create_app  # noqa: E402

from services import startup, transcode  # noqa: E402
from services.config import data_dir  # noqa: E402
from services.routes import router as media_router  # noqa: E402

APPLICATION = "lizardking"
HOST = "127.0.0.1"   # loopback only: this serves your files
PORT = int(os.getenv("LIZARDKING_PORT", "8777"))
KEY_COOKIE = "lizardking-key"
# Answered without the key. pytincture fetches its own framework files (the
# same public files in every pytincture app) with credentials "omit", so
# they never carry the cookie; nothing of yours is under these paths.
OPEN_PATHS = ("/lk/health", "/favicon.ico")
OPEN_PREFIXES = (f"/{APPLICATION}/frontend/", "/frontend/")


def access_key() -> str:
    """
    The key in the link LizardKing prints, kept in the data directory (0600).

    The server only listens on loopback, but every account on the machine can
    reach loopback. Like Jupyter's token, the key keeps them out: without it
    (or the cookie that the link sets) no page, BFF call or media URL answers.
    LIZARDKING_NO_KEY=1 turns this off for a single-user machine.
    """
    path = data_dir() / "access.key"
    try:
        key = path.read_text().strip()
        if len(key) >= 20:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(24)
    path.write_text(key)
    path.chmod(0o600)
    return key


def build_app():
    app = create_app(PytinctureConfig(
        modules_path=str(APPCODE),
        default_application=APPLICATION,
        allowed_hosts=(HOST,),
        canonical_origin=f"http://{HOST}:{PORT}",
        cookie_namespace=APPLICATION,
    ))
    app.include_router(media_router)
    app.mount("/lk/static", StaticFiles(directory=str(APPCODE / "static")), name="lk-static")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(f"/{APPLICATION}/")

    return app


def with_media_policy(inner):
    """
    pytincture's Content-Security-Policy has no media-src, so default-src
    'self' applies, and hls.js plays through blob: URLs: a transcoded file
    would be refused. Add media-src for blob: (and http/https for network
    streams), and let hls.js fetch network streams (connect-src).
    """
    async def asgi(scope, receive, send):
        async def patched(message):
            if message["type"] == "http.response.start":
                headers = []
                for key, value in message.get("headers", []):
                    if key.lower() == b"content-security-policy" and b"media-src" not in value:
                        value = value.replace(b"connect-src ", b"connect-src http: https: ", 1)
                        value += b"; media-src 'self' blob: http: https:"
                    headers.append((key, value))
                message = {**message, "headers": headers}
            await send(message)
        await inner(scope, receive, patched)
    return asgi


LOCKED_PAGE = b"""<!doctype html><meta charset="utf-8"><title>LizardKing</title>
<style>body{font:16px system-ui;background:#111;color:#ddd;display:grid;place-items:center;height:100vh;margin:0}
main{max-width:34em;padding:2em}code{background:#222;padding:.1em .3em;border-radius:3px}</style>
<main><h1>LizardKing is locked</h1><p>Open it from the link it printed when it started,
the one ending in <code>?key=&hellip;</code>. The key is also in
<code>~/.local/share/lizardking/access.key</code>.</p></main>"""


def with_access_key(inner, key: str):
    """Let in requests that carry the key cookie; trade a ?key= link for the cookie."""
    expected = key.encode()

    async def asgi(scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] not in ("http", "websocket") or path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
            return await inner(scope, receive, send)
        cookies = SimpleCookie()
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                try:
                    cookies.load(value.decode("latin-1"))
                except Exception:  # noqa: BLE001 - a malformed cookie is just absent
                    pass
        held = cookies.get(KEY_COOKIE)
        if held and hmac.compare_digest(held.value.encode(), expected):
            return await inner(scope, receive, send)
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1")))
        if scope["type"] == "http" and hmac.compare_digest(query.get("key", "").encode(), expected):
            rest = urlencode({k: v for k, v in query.items() if k != "key"})
            location = scope["path"] + (f"?{rest}" if rest else "")
            cookie = f"{KEY_COOKIE}={key}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", location.encode()), (b"set-cookie", cookie.encode()),
                                    (b"cache-control", b"no-store")]})
            return await send({"type": "http.response.body", "body": b""})
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 4401})
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": LOCKED_PAGE})
    return asgi


fastapi_app = build_app()
app = with_media_policy(fastapi_app)
if os.getenv("LIZARDKING_NO_KEY") != "1":
    app = with_access_key(app, access_key())


def main() -> None:
    import threading
    import time
    import urllib.request
    import webbrowser

    import uvicorn

    parser = argparse.ArgumentParser(prog="lizardking", description="A local media player in the browser.")
    parser.add_argument("paths", nargs="*", help="files, folders or playlists to queue")
    parser.add_argument("--no-browser", action="store_true", help="print the link but do not open it")
    args = parser.parse_args()
    startup.queue([os.path.abspath(os.path.expanduser(p)) for p in args.paths])

    url = f"http://{HOST}:{PORT}/{APPLICATION}/"
    if os.getenv("LIZARDKING_NO_KEY") != "1":
        url += f"?key={access_key()}"
    print(f"LizardKing: {url}", flush=True)

    if not args.no_browser:
        def open_when_up() -> None:
            for _ in range(100):
                try:
                    urllib.request.urlopen(f"http://{HOST}:{PORT}/lk/health", timeout=1)
                    break
                except OSError:
                    time.sleep(0.1)
            webbrowser.open(url)
        threading.Thread(target=open_when_up, daemon=True).start()

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level=os.getenv("LIZARDKING_LOG_LEVEL", "warning"))
    finally:
        transcode.sessions.shutdown()


if __name__ == "__main__":
    main()
