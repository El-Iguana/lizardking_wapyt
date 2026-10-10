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
