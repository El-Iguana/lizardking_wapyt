"""Segment plans, the playlist, the ffmpeg command line, and one real transcode."""
import subprocess
import time

import pytest

from conftest import needs_ffmpeg
from services import media, transcode


def test_segments_round_up_to_a_whole_frame():
    plan = transcode.plan_segments(60.0, 24000 / 1001)
    assert abs(plan.segment - 4.004) < 1e-6           # frame 96 at 23.976 fps
    assert plan.count == 15 and abs(plan.last - (60.0 - 14 * plan.segment)) < 1e-9


def test_no_frame_rate_uses_the_length_as_asked():
    plan = transcode.plan_segments(10.0)
    assert (plan.segment, plan.count, plan.last) == (4.0, 3, 2.0)


def test_a_sliver_is_not_a_segment():
    plan = transcode.plan_segments(8.01)
    assert plan.count == 2 and abs(plan.last - 4.01) < 1e-9


def test_no_duration_is_an_error():
    with pytest.raises(transcode.TranscodeError):
        transcode.plan_segments(0)


def test_vod_playlist_lists_every_segment_up_front():
    text = transcode.vod_playlist(transcode.plan_segments(10.0))
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text.rstrip().endswith("#EXT-X-ENDLIST")
    assert '#EXT-X-MAP:URI="init.mp4"' in text
    assert text.count("#EXTINF:") == 3 and "#EXTINF:2.000000," in text and "2.m4s" in text
    assert "#EXT-X-TARGETDURATION:4" in text


INFO = {"duration": 100.0,
        "video": [{"index": 0, "codec": "hevc", "height": 2160, "fps": 24.0, "attached_pic": False}],
        "audio": [{"index": 1, "codec": "ac3", "default": True}, {"index": 2, "codec": "aac", "default": False}],
        "subtitles": []}


def test_args_for_a_seek_seek_before_input_and_offset_the_output():
    plan = transcode.plan_segments(100.0, 24.0)
    args = transcode.ffmpeg_args("/f.mkv", INFO, 1, "/tmp/s", plan, start_number=5)
    assert args[args.index("-ss") + 1] == "20.000000" and args.index("-ss") < args.index("-i")
    assert args[args.index("-output_ts_offset") + 1] == "20.000000"
    assert args[args.index("-start_number") + 1] == "5"
    assert "-copyts" not in args
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["0:0", "0:2"]                       # the picture, and the chosen audio track
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*4.000000)"
    assert args[args.index("-vf") + 1] == "scale=-2:1080"   # software 4K does not keep up
    assert args[args.index("-hls_playlist_type") + 1] == "event"
    assert "temp_file" in args[args.index("-hls_flags") + 1]


def test_args_from_the_start_have_no_seek_and_nvenc_keeps_4k():
    plan = transcode.plan_segments(100.0, 24.0)
    args = transcode.ffmpeg_args("/f.mkv", INFO, None, "/tmp/s", plan, video_encoder="h264_nvenc")
    assert "-ss" not in args and "-output_ts_offset" not in args and "-vf" not in args
    assert args[args.index("-c:v") + 1] == "h264_nvenc" and "-forced-idr" in args
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["0:0", "0:1"]  # default audio


def test_surround_stays_surround_and_everything_else_is_stereo():
    plan = transcode.plan_segments(100.0, 24.0)

    def sound(channels):
        info = {**INFO, "audio": [{"index": 1, "codec": "dts", "default": True, "channels": channels}]}
        args = transcode.ffmpeg_args("/f.mkv", info, None, "/tmp/s", plan)
        return args[args.index("-ac") + 1], args[args.index("-b:a") + 1]

    assert sound(6) == ("6", "384k")                    # 5.1 kept
    assert sound(8) == ("6", "384k") == sound(7)        # 7.1 and 6.1 mixed down to 5.1
    assert sound(2) == ("2", "192k") == sound(1) == sound(None)
    assert sound(4) == ("2", "192k")                    # quad: no AAC layout every player takes


def test_audio_only_maps_no_picture():
    info = {"duration": 30.0, "video": [{"index": 1, "attached_pic": True}], "audio": [{"index": 0, "codec": "wmav2"}]}
    args = transcode.ffmpeg_args("/a.wma", info, None, "/tmp/s", transcode.plan_segments(30.0))
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["0:0"]
    assert "-c:v" not in args


@needs_ffmpeg
def test_a_real_transcode_serves_segments_and_restarts_on_a_seek(tmp_path, monkeypatch):
    monkeypatch.setenv("LIZARDKING_ENCODER", "libx264")
    monkeypatch.setattr(transcode, "_encoder", None)
    src = tmp_path / "clip.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "30", "-c:v", "mpeg4", "-c:a", "mp2", str(src)],
                   check=True)
    info = media.probe(str(src))
    assert media.plan(info, ".mkv", {"mkv": True})["mode"] == "transcode"
    sessions = transcode.Sessions(tmp_path / "sessions")
    try:
        s = sessions.open(str(src), info)
        assert sessions.open(str(src), info) is s       # one session per file and track
        assert s.plan.count == 8
        assert sessions.segment(s, "init.mp4").stat().st_size > 0
        first = sessions.segment(s, "0.m4s")
        assert first.stat().st_size > 0 and s.started_at == 0
        late = sessions.segment(s, "6.m4s")              # far ahead: the encoder restarts there
        assert late.exists() and s.started_at == 6
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time", "-of", "csv=p=0",
                                f"concat:{tmp_path / 'sessions' / s.id / 'init.mp4'}|{late}"],
                               capture_output=True, text=True)
        assert abs(float(probe.stdout.strip()) - 24.0) < 0.2   # the segment sits at 24 s in the film
        with pytest.raises(FileNotFoundError):
            sessions.segment(s, "99.m4s")
    finally:
        sessions.shutdown()
    time.sleep(0.1)
    assert sessions.running_count() == 0


# Image subtitles ---------------------------------------------------------------

PGS = {"index": 3, "codec": "hdmv_pgs_subtitle", "image": True, "text": False}
WITH_PGS = {**INFO, "video": [{**INFO["video"][0], "width": 1920, "height": 804}],
            "subtitles": [{"index": 2, "codec": "subrip", "text": True, "image": False}, PGS]}


def test_an_image_subtitle_is_a_reason_to_transcode():
    info = {**WITH_PGS, "format": "mov,mp4,m4a,3gp,3g2,mj2",
            "video": [{"index": 0, "codec": "h264", "pix_fmt": "yuv420p", "attached_pic": False}],
            "audio": [{"index": 1, "codec": "aac", "default": True}]}
    assert media.plan(info, ".mp4")["mode"] == "direct"
    assert media.plan(info, ".mp4", burn=3) == {"mode": "transcode", "reasons": ["image subtitles drawn into the picture"]}
    assert media.plan(info, ".mp4", burn=2)["mode"] == "direct"      # a text subtitle is a <track>, not a burn
    assert media.plan(info, ".mp4", burn=9)["mode"] == "direct"      # no such stream
    assert media.burnable({**info, "video": []}, 3) is None          # nothing to draw into


def test_a_burned_subtitle_is_fitted_inside_the_picture_and_overlaid():
    plan = transcode.plan_segments(100.0, 24.0)
    args = transcode.ffmpeg_args("/f.mkv", WITH_PGS, None, "/tmp/s", plan, video_encoder="h264_nvenc", burn=3)
    assert args[args.index("-filter_complex") + 1] == (
        "[0:3]scale=1920:804:force_original_aspect_ratio=decrease[sub];"
        "[0:0][sub]overlay=(W-w)/2:(H-h)/2:eof_action=pass[v]")
    assert [args[i + 1] for i, a in enumerate(args) if a == "-map"] == ["[v]", "0:1"]
    assert "-vf" not in args and args.index("-filter_complex") > args.index("-i")
    assert "-filter_complex" not in transcode.ffmpeg_args("/f.mkv", WITH_PGS, None, "/tmp/s", plan, burn=2)


def test_a_software_4k_burn_scales_down_before_the_overlay():
    info = {**WITH_PGS, "video": [{**INFO["video"][0], "width": 3840, "height": 1604}]}
    args = transcode.ffmpeg_args("/f.mkv", info, None, "/tmp/s", transcode.plan_segments(100.0, 24.0), burn=3)
    assert args[args.index("-filter_complex") + 1] == (
        "[0:3]scale=2586:1080:force_original_aspect_ratio=decrease[sub];"       # fitted to the picture as it will be
        "[0:0]scale=-2:1080[pic];[pic][sub]overlay=(W-w)/2:(H-h)/2:eof_action=pass[v]") and "-vf" not in args


HDR = {**INFO, "video": [{**INFO["video"][0], "width": 3840, "height": 1604, "hdr": True, "transfer": "smpte2084"}]}


def test_an_hdr_picture_is_tone_mapped_and_an_sdr_one_left_alone():
    plan = transcode.plan_segments(100.0, 24.0)
    vf = lambda info, **more: (lambda a: a[a.index("-vf") + 1] if "-vf" in a else None)(
        transcode.ffmpeg_args("/f.mkv", info, None, "/tmp/s", plan, **more))
    mapped = vf(HDR, video_encoder="h264_nvenc", tone=True)
    assert mapped.startswith("zscale=tin=smpte2084:min=bt2020nc:pin=bt2020:rin=tv:t=linear") and "tonemap=hable" in mapped
    assert mapped.endswith("zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    # In software the picture is made smaller first: a quarter of the pixels to tone map.
    assert vf(HDR, tone=True) == "scale=-2:1080," + mapped
    hlg = {**HDR, "video": [{**HDR["video"][0], "transfer": "arib-std-b67"}]}
    assert "tin=arib-std-b67" in vf(hlg, video_encoder="h264_nvenc", tone=True)
    assert vf(HDR, video_encoder="h264_nvenc") is None            # an ffmpeg without zscale: as before
    assert vf(INFO, video_encoder="h264_nvenc", tone=True) is None


def test_a_burned_subtitle_goes_over_the_tone_mapped_picture():
    info = {**WITH_PGS, "video": HDR["video"]}
    graph = (lambda a: a[a.index("-filter_complex") + 1])(transcode.ffmpeg_args(
        "/f.mkv", info, None, "/tmp/s", transcode.plan_segments(100.0, 24.0), video_encoder="h264_nvenc", burn=3, tone=True))
    assert graph.startswith("[0:3]scale=3840:1604:force_original_aspect_ratio=decrease[sub];[0:0]zscale=tin=smpte2084")
    assert graph.endswith("format=yuv420p[pic];[pic][sub]overlay=(W-w)/2:(H-h)/2:eof_action=pass[v]")


@needs_ffmpeg
def test_a_real_transcode_tone_maps_hdr(tmp_path, monkeypatch):
    if not transcode.tone_mapping():
        pytest.skip("this ffmpeg has no zscale")
    monkeypatch.setenv("LIZARDKING_ENCODER", "libx264")
    monkeypatch.setattr(transcode, "_encoder", None)
    # Ten-bit PQ in BT.2020: a grey at three quarters of the signal, which is about 1000 nits.
    src = tmp_path / "hdr.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xBFBFBF:size=320x240:rate=25",
                    "-t", "8", "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
                    "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", str(src)],
                   check=True, capture_output=True)
    info = media.probe(str(src))
    assert media.picture(info)["hdr"] and media.picture(info)["transfer"] == "smpte2084"

    def grey(tone):
        monkeypatch.setattr(transcode, "_tone_mapping", tone)
        sessions = transcode.Sessions(tmp_path / f"sessions-{tone}")
        try:
            s = sessions.open(str(src), info)
            sessions.segment(s, "init.mp4")
            segment = sessions.segment(s, "0.m4s")
            shown = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=pix_fmt,color_transfer", "-of", "csv=p=0", str(s.dir / "ffmpeg.m3u8")],
                                   capture_output=True, text=True).stdout.strip()
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", f"concat:{s.dir / 'init.mp4'}|{segment}", "-ss", "1",
                                  "-frames:v", "1", "-vf", "crop=2:2:160:120", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                                 capture_output=True, check=True).stdout
            return raw[0], shown
        finally:
            sessions.shutdown()

    level, shown = grey(False)
    assert 180 < level < 200 and "smpte2084" in shown      # squeezed into eight bits as it is: the film's white is grey
    level, shown = grey(True)
    assert level > 240 and "bt709" in shown and "smpte2084" not in shown      # white again, and said to be SDR


def test_each_burned_subtitle_is_its_own_session(tmp_path):
    sessions = transcode.Sessions(tmp_path / "sessions")
    plain, burned = sessions.open("/f.mkv", WITH_PGS), sessions.open("/f.mkv", WITH_PGS, None, 3)
    assert plain is not burned and burned.burn == 3 and sessions.open("/f.mkv", WITH_PGS, None, 3) is burned
    sessions.shutdown()


@needs_ffmpeg
def test_a_real_transcode_draws_a_pgs_subtitle_into_the_picture(tmp_path, monkeypatch):
    import pgs
    monkeypatch.setenv("LIZARDKING_ENCODER", "libx264")
    monkeypatch.setattr(transcode, "_encoder", None)
    # A Blu-ray canvas over a film cropped to 2.39:1: a box low in the canvas, from 0 s to 10 s and 22 s to 27 s.
    (tmp_path / "subs.sup").write_bytes(pgs.sup([(0, 10, 760, 900, 400, 60), (22, 27, 760, 900, 400, 60)]))
    src = tmp_path / "film.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:size=960x402:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-i", str(tmp_path / "subs.sup"), "-t", "30",
                    "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-c:s", "copy", str(src)], check=True)
    info = media.probe(str(src))
    stream = next(s["index"] for s in info["subtitles"] if s["image"])
    assert media.plan(info, ".mkv", {"mkv": True}, burn=stream)["mode"] == "transcode"

    def pixel(sessions, session, name, after=1):
        """The colour where the box belongs, ``after`` seconds into a segment: the canvas
        is 715x402 here, centred, so the box's middle is (480, 346)."""
        segment = sessions.segment(session, name)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", f"concat:{session.dir / 'init.mp4'}|{segment}",
                              "-ss", str(after), "-frames:v", "1", "-vf", "crop=2:2:480:346", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        return tuple(raw[:3])

    sessions, seeked = transcode.Sessions(tmp_path / "sessions"), transcode.Sessions(tmp_path / "seeked")
    try:
        burned = sessions.open(str(src), info, None, stream)
        sessions.segment(burned, "init.mp4")
        r, g, b = pixel(sessions, burned, "0.m4s")
        assert r > 200 and g < 90 and b > 200                    # magenta: the caption is in the picture
        assert max(pixel(sessions, burned, "7.m4s")) < 40        # 29 s: the caption has gone
        plain = sessions.open(str(src), info)
        sessions.segment(plain, "init.mp4")
        assert max(pixel(sessions, plain, "0.m4s")) < 40         # and it is not there unless asked for
        # An encoder started at 20 s (a seek) draws the next caption when it is due, at 22 s.
        late = seeked.open(str(src), info, None, stream)
        with late.lock:
            seeked._start(late, 5)
        assert late.started_at == 5 and max(pixel(seeked, late, "5.m4s", after=1)) < 40
        r, g, b = pixel(seeked, late, "5.m4s", after=3)
        assert r > 200 and g < 90 and b > 200
    finally:
        sessions.shutdown()
        seeked.shutdown()
