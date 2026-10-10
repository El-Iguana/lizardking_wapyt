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
import mimetypes
import os
import signal
import sys
from pathlib import Path

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
from services.routes import router as media_router  # noqa: E402

APPLICATION = "lizardking"
HOST = "127.0.0.1"   # loopback only: this serves your files
PORT = int(os.getenv("LIZARDKING_PORT", "8777"))


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


def with_localhost_redirect(inner):
    """
    Send http://localhost:PORT/... to http://127.0.0.1:PORT/... .

    pytincture answers only the canonical host (anything else gets a bare
    "Invalid host header", which looks like a blank page), and its Origin
    checks are pinned to 127.0.0.1, so localhost cannot simply be allowed.
    """
    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            host = dict(scope.get("headers", [])).get(b"host", b"").decode("latin-1")
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            if name.lower() in ("localhost", "[::1]"):
                query = scope.get("query_string", b"").decode("latin-1")
                location = f"http://{HOST}:{PORT}{scope.get('raw_path', b'').decode('latin-1') or scope['path']}"
                location += f"?{query}" if query else ""
                await send({"type": "http.response.start", "status": 307,
                            "headers": [(b"location", location.encode("latin-1")), (b"cache-control", b"no-store")]})
                return await send({"type": "http.response.body", "body": b""})
        await inner(scope, receive, send)
    return asgi


fastapi_app = build_app()
app = with_localhost_redirect(with_media_policy(fastapi_app))


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

    # uvicorn re-raises the signal that stopped it once it has shut down. For
    # SIGTERM (kill, logout) the default action ends the process on the spot,
    # skipping the finally below and leaving ffmpeg running: make it unwind
    # like Ctrl+C instead.
    def stop_on_sigterm(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_on_sigterm)

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level=os.getenv("LIZARDKING_LOG_LEVEL", "warning"))
    finally:
        transcode.sessions.shutdown()


if __name__ == "__main__":
    main()
