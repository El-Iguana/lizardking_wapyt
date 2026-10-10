"""Video thumbnails for the Open dialog: where the frame is taken, the command, the cache, the fallback."""
import subprocess

import pytest

from conftest import needs_ffmpeg
from services import dlna, routes, thumbs, tokens


def test_the_frame_is_a_tenth_in_capped_and_early_when_unknown():
    assert thumbs.seek_for(600) == 60
    assert thumbs.seek_for(3 * 3600) == thumbs.SEEK_MAX
    assert thumbs.seek_for(None) == thumbs.SEEK_UNKNOWN


def test_one_frame_seeked_before_the_input_and_reconnecting_for_a_url():
    args = thumbs.ffmpeg_args("http://10.0.0.2:8200/MediaItems/1.mkv", 42.5)
    assert args.index("-ss") < args.index("-i") and args[args.index("-ss") + 1] == "42.500"
    assert "-reconnect" in args and args[args.index("-frames:v") + 1] == "1"
    assert "-reconnect" not in thumbs.ffmpeg_args("/films/a.mkv", 1)


def test_a_rescanned_server_reusing_an_address_gets_a_new_thumbnail():
    url = "http://10.0.0.2:8200/MediaItems/1.mkv"
    assert thumbs.key(url, 100, 60.0) != thumbs.key(url, 200, 60.0)


@needs_ffmpeg
def test_a_real_frame_is_made_once_and_then_read_from_disk(tmp_path, monkeypatch):
    film = tmp_path / "film.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=20:size=640x360:rate=25",
                    "-pix_fmt", "yuv420p", str(film)], check=True)
    monkeypatch.setattr(thumbs, "_memory", {})
    body = thumbs.thumbnail(str(film), 1, 20.0)
    assert body.startswith(b"\xff\xd8")
    monkeypatch.setattr(thumbs, "_memory", {})
    monkeypatch.setattr(thumbs, "_make", lambda *_: pytest.fail("made it again"))
    assert thumbs.thumbnail(str(film), 1, 20.0) == body


def test_a_video_without_a_frame_shows_the_lizardking(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbs, "_memory", {})
    monkeypatch.setattr(thumbs, "_make", lambda *_: b"")
    response = routes.thumb(tokens.token_for(str(tmp_path / "broken.mkv")))
    assert response.media_type == "image/webp" and response.body == routes.NO_THUMB.read_bytes()


def test_thumbnails_only_from_a_known_server(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(dlna, "_servers", {})
    monkeypatch.setattr(dlna, "discover", lambda force=False: [])
    with pytest.raises(HTTPException) as refused:
        routes.thumb(tokens.token_for("http://203.0.113.9/MediaItems/1.mkv"))
    assert refused.value.status_code == 403
