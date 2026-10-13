"""
On-demand HLS transcoding for files the browser cannot play as they are.

The design is Orpheus's, whose measurements are quoted where they decided
something:

* The server writes the **whole** playlist before encoding anything: every
  segment, the real duration and ENDLIST. The player shows the full length and
  can seek anywhere at once. ffmpeg's own playlist only lists what it has
  encoded so far; it is read solely to learn which segments are finished.
* A segment nobody has encoded yet either is close ahead of the encoder (wait
  for it) or is somewhere else (the viewer seeked): the encoder is restarted
  there with ``-ss`` before ``-i``, ``-start_number`` and ``-output_ts_offset``.
* Video is always re-encoded, with a keyframe forced on every boundary, so each
  segment is exactly as long as the playlist says. A copied stream can only be
  cut on its own keyframes (copy-remux is on the roadmap).
* Sound keeps its surround: a track with six channels or more becomes 5.1 AAC
  (which the player decodes as six channels, measured in Chromium), anything
  else stereo. 7.1 is mixed down to 5.1: 5.1 is the widest AAC layout every
  player takes.
* ``-pix_fmt yuv420p`` always: 10-bit H.264 plays as sound and a black picture
  on hardware decoders.
* The playlist type is ``event``: with ``vod`` ffmpeg holds its playlist back
  until the end; with none it is a live stream.
* ``temp_file``: a restarted encoder rewrites segments that already exist, and
  writing each to a temporary name first means one being served is never
  half-replaced.

Plain module: the sessions live here for the life of the process.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from services import media
from services.config import cache_dir, ffmpeg_bin

SEGMENT_SECONDS = 4.0
SEGMENTS_AHEAD = 4          # wait for a segment this close ahead of the encoder
WAIT_SECONDS = 45.0
IDLE_SECONDS = 120.0        # stop an encoder nobody has asked anything of
MAX_RUNNING = 2             # encoders at once; the oldest idle one is stopped
AUDIO_BITRATE = {2: "192k", 6: "384k"}     # by channels out
INIT_NAME = "init.mp4"
FFMPEG_PLAYLIST = "ffmpeg.m3u8"
SEGMENT_RE = re.compile(r"^(\d+)\.m4s$")


class TranscodeError(Exception):
    pass


# Segments ---------------------------------------------------------------------

@dataclass(frozen=True)
class SegmentPlan:
    segment: float
    count: int
    last: float
    total: float

    def start(self, n: int) -> float:
        return n * self.segment

    def length(self, n: int) -> float:
        return self.last if n == self.count - 1 else self.segment


def plan_segments(duration: float, fps: float = 0.0, seconds: float = SEGMENT_SECONDS) -> SegmentPlan:
    """
    Cut ``duration`` into segments of ``seconds``, rounded up to a whole frame.

    A keyframe can only fall on a frame: four seconds of 24000/1001 video is
    frame 96, at 4.004 s. Writing 4.000 in the playlist would drift by 11 s
    over a three-hour film (Orpheus, measured).
    """
    if not duration or duration <= 0:
        raise TranscodeError("no duration")
    segment = math.ceil(seconds * fps - 1e-9) / fps if fps > 0 else seconds
    count = max(1, math.ceil(duration / segment - 1e-9))
    last = duration - (count - 1) * segment
    if last < 0.05 and count > 1:  # a sliver shorter than a frame is not a segment
        count -= 1
        last = duration - (count - 1) * segment
    return SegmentPlan(segment=segment, count=count, last=last, total=duration)


def vod_playlist(plan: SegmentPlan) -> str:
    lines = [
        "#EXTM3U", "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{math.ceil(max(plan.segment, plan.last))}",
        "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS",
        f'#EXT-X-MAP:URI="{INIT_NAME}"',
    ]
    for n in range(plan.count):
        lines += [f"#EXTINF:{plan.length(n):.6f},", f"{n}.m4s"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


# Encoder ----------------------------------------------------------------------

_encoder: Optional[str] = None
_encoder_lock = threading.Lock()


def encoder() -> str:
    """
    ``h264_nvenc`` when it actually encodes on this machine, else ``libx264``.

    LIZARDKING_ENCODER forces one. ``auto`` tries NVENC once with a tiny test
    encode: being listed by ``ffmpeg -encoders`` proves nothing without a GPU.
    """
    global _encoder
    with _encoder_lock:
        if _encoder:
            return _encoder
        wanted = os.getenv("LIZARDKING_ENCODER", "auto")
        if wanted != "auto":
            _encoder = wanted
            return _encoder
        _encoder = "libx264"
        try:
            test = subprocess.run(
                [ffmpeg_bin(), "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=size=256x256:duration=0.2",
                 "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=20)
            if test.returncode == 0:
                _encoder = "h264_nvenc"
        except (OSError, subprocess.TimeoutExpired):
            pass
        return _encoder


def audio_channels(track: dict) -> int:
    """Channels a transcode of this track has: 5.1 for surround, else stereo."""
    return 6 if (track.get("channels") or 0) >= 6 else 2


def ffmpeg_args(path: str, info: dict, audio: Optional[int], out_dir: str, plan: SegmentPlan,
                start_number: int = 0, video_encoder: str = "libx264") -> list[str]:
    """The whole command line for one run, starting at segment ``start_number``. Pure."""
    start_at = plan.start(start_number)
    args = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-v", "error"]
    if start_at > 0:
        # Before -i: seek the input instead of decoding everything up to here.
        args += ["-ss", f"{start_at:.6f}"]
    args += [*media.input_args(path), "-i", path]
    picture = media.picture(info)
    if picture:
        args += ["-map", f"0:{picture['index']}"]
        if video_encoder == "h264_nvenc":
            args += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0", "-forced-idr", "1"]
        else:
            args += ["-c:v", video_encoder, "-preset", "veryfast", "-crf", "21"]
            if (picture.get("height") or 0) > 1080:
                # A software 4K encode does not keep up with playback.
                args += ["-vf", "scale=-2:1080"]
        args += ["-pix_fmt", "yuv420p", "-profile:v", "high",
                 "-force_key_frames", f"expr:gte(t,n_forced*{plan.segment:.6f})"]
    tracks = info.get("audio", [])
    chosen = media.default_audio(info) if audio is None else audio
    if chosen is not None and 0 <= chosen < len(tracks):
        channels = audio_channels(tracks[chosen])
        # -ac also settles the layout: DTS's 5.1(side) becomes the 5.1 AAC has.
        args += ["-map", f"0:{tracks[chosen]['index']}", "-c:a", "aac", "-b:a", AUDIO_BITRATE[channels],
                 "-ac", str(channels)]
    args += ["-sn", "-dn"]
    if start_at > 0:
        # Timestamps restart at zero after a seek; this puts them back. Never
        # with -copyts, which applies the offset twice (Orpheus, measured).
        args += ["-output_ts_offset", f"{start_at:.6f}"]
    args += [
        "-f", "hls", "-hls_time", f"{plan.segment:.6f}", "-start_number", str(start_number),
        "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", INIT_NAME,
        "-hls_playlist_type", "event", "-hls_flags", "independent_segments+temp_file", "-hls_list_size", "0",
        "-hls_segment_filename", os.path.join(out_dir, "%d.m4s"), os.path.join(out_dir, FFMPEG_PLAYLIST),
    ]
    return args


# Sessions ---------------------------------------------------------------------

@dataclass
class Session:
    id: str
    path: str
    audio: Optional[int]
    info: dict
    plan: SegmentPlan
    dir: Path
    proc: Optional[subprocess.Popen] = None
    started_at: int = 0
    done: set = field(default_factory=set)
    last_access: float = field(default_factory=time.monotonic)
    stderr: deque = field(default_factory=lambda: deque(maxlen=20))
    lock: threading.RLock = field(default_factory=threading.RLock)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def finished(self, n: int) -> bool:
        """Whether segment n is whole: ffmpeg lists a segment once it has closed it."""
        if n in self.done:
            return True
        try:
            body = (self.dir / FFMPEG_PLAYLIST).read_text()
        except OSError:
            return False
        for line in body.splitlines():
            m = SEGMENT_RE.match(line.strip())
            if m:
                self.done.add(int(m.group(1)))
        return n in self.done

    def will_reach(self, n: int) -> bool:
        if not self.running() or n < self.started_at:
            return False
        ahead = max((k for k in self.done if k >= self.started_at), default=self.started_at - 1) + 1
        return n <= ahead + SEGMENTS_AHEAD


class Sessions:
    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        self._by_id: dict[str, Session] = {}
        self._by_key: dict[tuple[str, Optional[int]], Session] = {}
        self._lock = threading.Lock()
        self._reaper: Optional[threading.Thread] = None
        self._stopping = threading.Event()

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = cache_dir() / "transcode"
            shutil.rmtree(self._root, ignore_errors=True)  # nothing survives a restart
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def open(self, path: str, info: dict, audio: Optional[int] = None) -> Session:
        """The session for a file and audio track, made on first use (nothing starts yet)."""
        key = (path, audio)
        with self._lock:
            s = self._by_key.get(key)
            if s is None:
                plan = plan_segments(info["duration"], (media.picture(info) or {}).get("fps") or 0.0)
                sid = secrets.token_urlsafe(12)
                s = Session(id=sid, path=path, audio=audio, info=info, plan=plan, dir=self.root / sid)
                self._by_id[sid], self._by_key[key] = s, s
            s.last_access = time.monotonic()
            self._start_reaper()
            return s

    def get(self, sid: str) -> Optional[Session]:
        with self._lock:
            s = self._by_id.get(sid)
        if s:
            s.last_access = time.monotonic()
        return s

    def _start(self, s: Session, n: int) -> None:
        """(Re)start the encoder at segment n. Caller holds s.lock."""
        self._stop_proc(s)
        self._make_room(s)
        s.dir.mkdir(parents=True, exist_ok=True)
        (s.dir / FFMPEG_PLAYLIST).unlink(missing_ok=True)
        args = ffmpeg_args(s.path, s.info, s.audio, str(s.dir), s.plan, n, encoder())
        s.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        s.started_at = n
        s.stderr.clear()
        proc = s.proc

        def drain() -> None:
            for line in proc.stderr:
                s.stderr.append(line.decode(errors="replace").rstrip())
        threading.Thread(target=drain, daemon=True).start()

    def _make_room(self, s: Session) -> None:
        with self._lock:
            running = [o for o in self._by_id.values() if o is not s and o.running()]
        running.sort(key=lambda o: o.last_access)
        while len(running) >= MAX_RUNNING:
            self._stop_proc(running.pop(0))

    @staticmethod
    def _stop_proc(s: Session) -> None:
        if s.proc and s.proc.poll() is None:
            s.proc.terminate()
            try:
                s.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                s.proc.kill()
        s.proc = None

    def segment(self, s: Session, name: str) -> Path:
        """The path of init.mp4 or N.m4s, encoding it first if need be. Blocks."""
        if name == INIT_NAME:
            with s.lock:
                if not s.running() and not (s.dir / INIT_NAME).exists():
                    self._start(s, 0)
            self._wait(s, lambda: _has_bytes(s.dir / INIT_NAME) and (s.finished(s.started_at) or not s.running()))
            if not _has_bytes(s.dir / INIT_NAME):
                raise TranscodeError(self._why(s))
            return s.dir / INIT_NAME
        m = SEGMENT_RE.match(name)
        if not m or int(m.group(1)) >= s.plan.count:
            raise FileNotFoundError(name)
        n = int(m.group(1))
        with s.lock:
            if not s.finished(n) and not s.will_reach(n):
                self._start(s, n)
        self._wait(s, lambda: s.finished(n))
        if not s.finished(n):
            raise TranscodeError(self._why(s))
        return s.dir / name

    def _wait(self, s: Session, ready) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            if ready():
                return
            if not s.running():
                if ready():
                    return
                break
            s.last_access = time.monotonic()
            time.sleep(0.05)

    @staticmethod
    def _why(s: Session) -> str:
        tail = [line for line in s.stderr if line.strip()]
        return tail[-1] if tail else "the encoder stopped without saying why"

    def _start_reaper(self) -> None:
        if self._reaper and self._reaper.is_alive():
            return
        self._reaper = threading.Thread(target=self._reap, daemon=True, name="lizardking-reaper")
        self._reaper.start()

    def _reap(self) -> None:
        while not self._stopping.wait(10):
            now = time.monotonic()
            with self._lock:
                sessions = list(self._by_id.values())
            for s in sessions:
                if now - s.last_access > IDLE_SECONDS and (s.proc or s.dir.exists()):
                    # Stopped, not forgotten: the next request restarts it, so a
                    # long pause does not break the player's URL.
                    with s.lock:
                        self._stop_proc(s)
                        shutil.rmtree(s.dir, ignore_errors=True)
                        s.done.clear()

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._by_id.values() if s.running())

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            sessions = list(self._by_id.values())
        for s in sessions:
            self._stop_proc(s)
        if self._root:
            shutil.rmtree(self._root, ignore_errors=True)


def _has_bytes(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


sessions = Sessions()
