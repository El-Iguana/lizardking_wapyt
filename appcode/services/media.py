"""
What a file contains (ffprobe), and whether the browser can play it as it is.

``normalize`` and ``plan`` are pure, so the decision can be tested without a
single media file. ``probe`` runs ffprobe and caches by path, size and mtime.

The decision is deliberately conservative. A file that *might* play directly
but does not costs the viewer an error and a retry (the page falls back to a
transcode on a decode error), while a file transcoded needlessly only costs
CPU. So anything not on the lists below is transcoded.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from fractions import Fraction
from typing import Any, Optional
from urllib.parse import urlsplit

from services.config import ffprobe_bin

# Codecs every Chromium and Firefox decode, before asking the browser.
DIRECT_VIDEO = {"h264", "vp8", "vp9"}
DIRECT_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
# Codecs that depend on the browser and the machine: the page reports which it
# has (``caps``), measured with MediaSource.isTypeSupported / canPlayType.
OPTIONAL_VIDEO = {"hevc": "hevc", "av1": "av1"}
OPTIONAL_AUDIO = {"ac3": "ac3", "eac3": "eac3"}
PCM_PREFIX = "pcm_"
TEXT_SUBTITLES = {"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd"}
# Pictures, not text: they can only be drawn into the video (transcode.py).
IMAGE_SUBTITLES = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}

# ffprobe's format_name for each container family.
MP4_FAMILY = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}


def _fps(rate: Optional[str]) -> float:
    try:
        value = float(Fraction(rate)) if rate and rate != "0/0" else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0
    return value if 0 < value < 1000 else 0.0


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None  # drop NaN


def normalize(raw: dict) -> dict:
    """ffprobe's JSON reduced to what the player and the info dialog use."""
    fmt = raw.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    info: dict = {
        "format": fmt.get("format_name", ""),
        "format_long": fmt.get("format_long_name", ""),
        "duration": _float(fmt.get("duration")),
        "bit_rate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        "size": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else None,
        "tags": {k: tags[k] for k in ("title", "artist", "album", "album_artist", "date", "track", "genre") if k in tags},
        "video": [], "audio": [], "subtitles": [],
    }
    for s in raw.get("streams") or []:
        kind = s.get("codec_type")
        stags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disposition = s.get("disposition") or {}
        base = {
            "index": s.get("index"),
            "codec": s.get("codec_name", ""),
            "codec_long": s.get("codec_long_name", ""),
            "language": stags.get("language"),
            "title": stags.get("title"),
            "default": bool(disposition.get("default")),
        }
        if kind == "video":
            base.update({
                "profile": s.get("profile"),
                "width": s.get("width"), "height": s.get("height"),
                "pix_fmt": s.get("pix_fmt", ""),
                "fps": _fps(s.get("avg_frame_rate")) or _fps(s.get("r_frame_rate")),
                "bit_rate": int(s["bit_rate"]) if str(s.get("bit_rate", "")).isdigit() else None,
                "hdr": s.get("color_transfer") in HDR_TRANSFERS,
                # Cover art in an audio file is a "video" stream of one frame.
                "attached_pic": bool(disposition.get("attached_pic")),
            })
            info["video"].append(base)
        elif kind == "audio":
            base.update({
                "channels": s.get("channels"),
                "channel_layout": s.get("channel_layout"),
                "sample_rate": int(s["sample_rate"]) if str(s.get("sample_rate", "")).isdigit() else None,
                "bit_rate": int(s["bit_rate"]) if str(s.get("bit_rate", "")).isdigit() else None,
            })
            info["audio"].append(base)
        elif kind == "subtitle":
            base.update({
                "text": base["codec"] in TEXT_SUBTITLES,
                "image": base["codec"] in IMAGE_SUBTITLES,
                "forced": bool(disposition.get("forced")),
            })
            info["subtitles"].append(base)
    return info


def picture(info: dict) -> Optional[dict]:
    """The stream that is watched: the first video that is not cover art."""
    return next((v for v in info.get("video", []) if not v.get("attached_pic")), None)


def kind(info: dict) -> str:
    return "video" if picture(info) else "audio"


def _container_ok(info: dict, ext: str, caps: dict) -> tuple[bool, str]:
    names = set((info.get("format") or "").split(","))
    if names & MP4_FAMILY:
        return True, ""
    if "matroska" in names or "webm" in names:
        # One format name covers both; only the extension says which it is.
        if ext == ".webm" or caps.get("mkv"):
            return True, ""
        return False, "this browser does not open Matroska (.mkv) files"
    if names & {"ogg", "mp3", "flac", "wav", "aac"}:
        return True, ""
    return False, f"the {info.get('format_long') or info.get('format') or 'unknown'} container"


def _video_ok(v: dict, caps: dict) -> tuple[bool, str]:
    codec = v.get("codec", "")
    if codec == "h264":
        # Ten-bit H.264 (High 10) decodes in software but not in hardware:
        # measured in Orpheus as sound with a black picture.
        if v.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
            return False, f"H.264 in {v.get('pix_fmt') or 'an unusual pixel format'}"
        return True, ""
    if codec in DIRECT_VIDEO:
        return True, ""
    if codec in OPTIONAL_VIDEO and caps.get(OPTIONAL_VIDEO[codec]):
        return True, ""
    return False, f"{(v.get('codec_long') or codec or 'unknown').split(' / ')[0]} video"


def _audio_ok(a: dict, caps: dict, container: str) -> tuple[bool, str]:
    codec = a.get("codec", "")
    if codec in DIRECT_AUDIO:
        return True, ""
    if codec.startswith(PCM_PREFIX) and "wav" in container.split(","):
        return True, ""
    if codec in OPTIONAL_AUDIO and caps.get(OPTIONAL_AUDIO[codec]):
        return True, ""
    return False, f"{(a.get('codec_long') or codec or 'unknown').split(' / ')[0]} audio"


def burnable(info: dict, stream: Optional[int]) -> Optional[dict]:
    """The image subtitle with ffprobe index ``stream``, if it can be drawn into this file's picture."""
    if stream is None or not picture(info):
        return None
    return next((s for s in info.get("subtitles", []) if s.get("image") and s.get("index") == stream), None)


def default_audio(info: dict) -> Optional[int]:
    """Position in ``info['audio']`` that a browser plays: the default track, else the first."""
    audio = info.get("audio", [])
    if not audio:
        return None
    return next((i for i, a in enumerate(audio) if a.get("default")), 0)


def plan(info: dict, ext: str, caps: Optional[dict] = None, audio: Optional[int] = None,
         burn: Optional[int] = None) -> dict:
    """
    How to play a file: ``{"mode": "direct" | "transcode", "reasons": [...]}``.

    ``audio`` is the chosen audio track (its position in ``info['audio']``).
    A browser plays only a file's default track, so choosing another one is
    itself a reason to transcode. So is ``burn``, an image subtitle (its
    ffprobe index) to draw into the picture: a browser cannot show one.
    """
    caps = caps or {}
    reasons: list[str] = []
    ok, why = _container_ok(info, ext.lower(), caps)
    if not ok:
        reasons.append(why)
    v = picture(info)
    if v:
        ok, why = _video_ok(v, caps)
        if not ok:
            reasons.append(why)
    chosen = default_audio(info) if audio is None else audio
    if chosen is not None and 0 <= chosen < len(info.get("audio", [])):
        ok, why = _audio_ok(info["audio"][chosen], caps, info.get("format", ""))
        if not ok:
            reasons.append(why)
        if chosen != default_audio(info):
            reasons.append(f"audio track {chosen + 1} chosen")
    if burnable(info, burn):
        reasons.append("image subtitles drawn into the picture")
    if not v and not info.get("audio"):
        return {"mode": "none", "reasons": ["no audio or video in this file"]}
    if reasons and not info.get("duration"):
        return {"mode": "none", "reasons": reasons + ["no duration, so it cannot be transcoded for seeking"]}
    return {"mode": "transcode" if reasons else "direct", "reasons": reasons}


# ffprobe, cached ------------------------------------------------------------

_cache: dict[tuple[str, int, int], dict] = {}
_cache_lock = threading.Lock()


class ProbeError(Exception):
    pass


def is_remote(path: str) -> bool:
    """An http(s) URL (a media server's item) rather than a local file."""
    return path.startswith(("http://", "https://"))


def extension(path: str) -> str:
    """The file extension of a path or of a URL's path."""
    if is_remote(path):
        path = urlsplit(path).path
    return os.path.splitext(path)[1]


def input_args(path: str) -> list[str]:
    """ffmpeg input options for ``path``: reconnect when a server drops an http read."""
    if is_remote(path):
        return ["-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5"]
    return []


def probe(path: str) -> dict:
    """ffprobe a file, cached until its size or mtime changes (a URL: for the life of the process)."""
    if is_remote(path):
        key = (path, 0, 0)
    else:
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        out = subprocess.run(
            [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", *input_args(path), path],
            capture_output=True, timeout=30, check=False,
        )
    except FileNotFoundError as error:
        raise ProbeError("ffprobe is not installed (it comes with ffmpeg)") from error
    except subprocess.TimeoutExpired as error:
        raise ProbeError("ffprobe took longer than 30 seconds") from error
    if out.returncode != 0:
        message = out.stderr.decode(errors="replace").strip().splitlines()
        raise ProbeError(message[-1] if message else "ffprobe could not read this file")
    info = normalize(json.loads(out.stdout or b"{}"))
    with _cache_lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[key] = info
    return info
