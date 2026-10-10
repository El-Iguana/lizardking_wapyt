"""The direct-play decision, without media files: ffprobe JSON in, plan out."""
from services import media


def probe_json(fmt="mov,mp4,m4a,3gp,3g2,mj2", streams=(), duration="60.0"):
    return {"format": {"format_name": fmt, "format_long_name": "QuickTime / MOV", "duration": duration,
                       "bit_rate": "1000000", "size": "123", "tags": {"TITLE": "A Film"}},
            "streams": list(streams)}


H264 = {"index": 0, "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080,
        "avg_frame_rate": "24000/1001", "disposition": {"default": 1}}
AAC = {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000",
       "disposition": {"default": 1}, "tags": {"language": "eng"}}
AC3 = {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6, "disposition": {"default": 0},
       "tags": {"language": "fra"}}


def plan(raw, ext=".mp4", caps=None, audio=None):
    return media.plan(media.normalize(raw), ext, caps or {}, audio)


def test_normalize_reads_streams_tags_and_frame_rate():
    info = media.normalize(probe_json(streams=[H264, AAC]))
    assert info["duration"] == 60.0 and info["tags"] == {"title": "A Film"}
    assert round(info["video"][0]["fps"], 3) == 23.976
    assert info["audio"][0]["language"] == "eng" and info["audio"][0]["default"]


def test_h264_aac_mp4_plays_directly():
    assert plan(probe_json(streams=[H264, AAC])) == {"mode": "direct", "reasons": []}


def test_ten_bit_h264_is_transcoded():
    ten = {**H264, "pix_fmt": "yuv420p10le"}
    result = plan(probe_json(streams=[ten, AAC]))
    assert result["mode"] == "transcode" and "yuv420p10le" in result["reasons"][0]


def test_hevc_depends_on_the_browser():
    hevc = {**H264, "codec_name": "hevc", "codec_long_name": "H.265 / HEVC (High Efficiency Video Coding)"}
    assert plan(probe_json(streams=[hevc, AAC]))["mode"] == "transcode"
    assert plan(probe_json(streams=[hevc, AAC]), caps={"hevc": True})["mode"] == "direct"


def test_matroska_needs_the_browser_to_say_so_but_webm_does_not():
    mkv = probe_json(fmt="matroska,webm", streams=[H264, AAC])
    assert plan(mkv, ".mkv")["mode"] == "transcode"
    assert plan(mkv, ".mkv", {"mkv": True})["mode"] == "direct"
    vp9 = {**H264, "codec_name": "vp9"}
    opus = {**AAC, "codec_name": "opus"}
    assert plan(probe_json(fmt="matroska,webm", streams=[vp9, opus]), ".webm")["mode"] == "direct"


def test_ac3_audio_is_transcoded_even_in_a_playable_container():
    # Chrome plays an AC-3 .mkv *silently* (measured), so this must not be direct.
    ac3 = {**AC3, "disposition": {"default": 1}}
    result = plan(probe_json(fmt="matroska,webm", streams=[H264, ac3]), ".mkv", {"mkv": True})
    assert result["mode"] == "transcode" and "audio" in result["reasons"][0].lower()


def test_choosing_a_non_default_audio_track_transcodes():
    raw = probe_json(streams=[H264, AAC, {**AC3, "codec_name": "aac"}])
    assert plan(raw)["mode"] == "direct"
    result = plan(raw, audio=1)
    assert result["mode"] == "transcode" and result["reasons"] == ["audio track 2 chosen"]


def test_cover_art_makes_an_audio_file_not_a_video():
    cover = {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}
    mp3 = {"index": 0, "codec_type": "audio", "codec_name": "mp3", "disposition": {"default": 1}}
    info = media.normalize(probe_json(fmt="mp3", streams=[mp3, cover]))
    assert media.kind(info) == "audio" and media.picture(info) is None
    assert media.plan(info, ".mp3", {})["mode"] == "direct"


def test_unusual_audio_is_transcoded():
    wma = {"index": 0, "codec_type": "audio", "codec_name": "wmav2", "codec_long_name": "Windows Media Audio 2"}
    result = plan(probe_json(fmt="asf", streams=[wma]), ".wma")
    assert result["mode"] == "transcode"
    assert any("Windows Media Audio 2" in r for r in result["reasons"])


def test_pcm_only_in_wav():
    pcm = {"index": 0, "codec_type": "audio", "codec_name": "pcm_s16le"}
    assert plan(probe_json(fmt="wav", streams=[pcm]), ".wav")["mode"] == "direct"
    assert plan(probe_json(fmt="aiff", streams=[pcm]), ".aiff")["mode"] == "transcode"


def test_no_duration_cannot_be_transcoded():
    wma = {"index": 0, "codec_type": "audio", "codec_name": "wmav2"}
    result = plan(probe_json(fmt="asf", streams=[wma], duration="N/A"), ".wma")
    assert result["mode"] == "none"


def test_no_streams_is_not_playable():
    assert plan(probe_json(streams=[]))["mode"] == "none"
