# LizardKing — specification

*LizardKing* is a local media player in the browser, in the spirit of VLC: open
any file on your disk and it plays. It is a pyTincture app: the page is Python
running in Pyodide, built from wapyt widgets, and the server is Python too. The
name is for Jim Morrison ("I am the Lizard King, I can do anything") and for El
Iguana.

This document is the design of **v0.1** and the reasoning behind it. What comes
after is in [ROADMAP.md](../ROADMAP.md).

## 1. Goals

1. **Plays anything ffmpeg can read**, from the local disk, with no conversion
   step and no library to build first.
2. **Direct when possible.** A file the browser can decode is served as it is,
   with HTTP Range for seeking. Nothing is re-encoded that does not have to be.
3. **Transcode when needed, without the viewer noticing more than a second's
   wait**: full duration shown at once, seek anywhere, pick another audio track.
4. **VLC's habits**: an Open dialog, a playlist, recent files, resume where you
   stopped, playback speed, jump to time, A→B loop, snapshot, fit and zoom,
   media information, and VLC's keyboard shortcuts where they do not clash.
5. **Local and private.** One user, one machine; nothing leaves it.

Not goals for v0.1: a media library with metadata scraping (that is Orpheus's
job), streaming to other devices, DASH, DRM.

## 2. Architecture

```
Browser (Pyodide)                              Server (CPython, 127.0.0.1)
appcode/lizardking.py ──BFF calls──────────▶  services/player_service.py
  MenuBar, MediaPlayer, Listbox, Modal          browse · prepare · info · positions
  │                                             recent · settings · playlists
  │ <video src=/lk/file/TOKEN>  ─────────────▶  routes.py  /lk/file   (Range)
  │ hls.js  /lk/hls/SID/…       ─────────────▶            /lk/hls    ──▶ transcode.py ──▶ ffmpeg
  │ <track  /lk/vtt/TOKEN>      ─────────────▶            /lk/vtt    ──▶ subtitles.py ──▶ ffmpeg
  │ poster  /lk/cover/TOKEN     ─────────────▶            /lk/cover  ──▶ ffmpeg
                                                media.py ──▶ ffprobe   store.py ──▶ state.json
```

* **BFF** (`PlayerService`): the page's API. pyTincture pins these calls to the
  app's own origin. Every export is a sync `def` (run in a worker thread) and
  the page calls the generated `*_async`.
* **Plain routes** for the media itself, because `<video>`, hls.js and
  `<track>` fetch URLs directly. They take an opaque **token** or **session id**,
  never a path: a path is registered through a BFF call and comes back as a
  random token (`tokens.py`), so no other web page can point a `<video>` at your
  files.
* **State that persists** (tokens, transcode sessions, the store, probe cache)
  lives in plain modules. pyTincture re-executes a BFF module on every call, so
  module-level state there would be new each time (`tests/test_bff.py`).

## 3. Direct or transcode (`media.plan`)

`ffprobe` once per file (cached by path, size and mtime), then a decision that
is a pure function of the probe, the extension, the browser's capabilities and
the chosen audio track:

| | Plays directly |
|---|---|
| Containers | MP4/MOV family, WebM, Ogg, MP3, FLAC, WAV, ADTS AAC; Matroska if the browser says so |
| Video | H.264 8-bit 4:2:0, VP8, VP9; HEVC and AV1 if the browser says so |
| Audio | AAC, MP3, Opus, Vorbis, FLAC; PCM in WAV; AC-3/E-AC-3 if the browser says so |

Everything else is transcoded, and the reasons are kept for the Media
Information dialog. Measured, not assumed:

* **Ten-bit H.264** decodes in software but plays as sound and a black picture
  on hardware decoders (Orpheus): transcoded.
* **Matroska**: Chrome answers `maybe` to `video/x-matroska` alone and
  `probably` with codecs, and does play H.264/AAC `.mkv` directly. So the page
  asks with codecs.
* **AC-3 in Matroska plays silently in Chrome**: picture, no sound, no error.
  AC-3 is therefore transcoded unless `MediaSource.isTypeSupported` says yes.
* Choosing an audio track other than the default **is itself a reason to
  transcode**: browsers play a file's default track only.

The page reports capabilities once (`lk.js` `caps()`): HEVC, AV1, Matroska,
AC-3 and E-AC-3. If a file the server sent directly still fails with a decode
error, the page asks for it again with `force_transcode` and carries on from
the same position (`MediaPlayer.refresh()`).

## 4. Transcoding (`transcode.py`)

HLS, fMP4 segments, produced on demand. The design is Orpheus's, whose
measurements decided each point:

* **The server writes the whole playlist first**: every segment, the real
  duration and `ENDLIST`, from the probe's duration and the segment length. The
  player shows the full length and can seek anywhere at once.
* **Segments are 4 s rounded up to a whole frame** (4.004 s at 23.976 fps), and
  a keyframe is forced on every boundary, so each segment is exactly as long as
  the playlist says. Video is therefore always re-encoded in v0.1.
* **A segment request** is either already done, close ahead of the encoder
  (wait), or elsewhere (the viewer seeked): ffmpeg is restarted there with `-ss`
  before `-i`, `-start_number N` and `-output_ts_offset`. Never `-copyts` with
  it, which applies the offset twice.
* **Finished** means listed in ffmpeg's own (event-type) playlist; a file that
  merely exists may be half-written. `temp_file` makes a restarted encoder
  replace segments atomically.
* **Always `yuv420p`**; ten-bit sources would otherwise come out as High 10.
* **Encoder**: NVENC when a one-off test encode succeeds, else libx264
  (`veryfast`, CRF 21, scaled to 1080p above that). `LIZARDKING_ENCODER` forces
  one.
* **Audio**: the chosen track to AAC stereo 192 kb/s.
* **Lifetime**: at most two encoders run; an idle one (2 min) is stopped and its
  files deleted, but the session stays, so a long pause does not break the
  player's URL: the next request restarts it. The cache is emptied at start and
  on shutdown.

## 5. Subtitles (`subtitles.py`)

WebVTT is the only format `<track>` takes, so everything goes through
`ffmpeg -f webvtt`:

* **Sidecar files** beside the video whose names start with its name: `.srt`,
  `.vtt`, `.ass`, `.ssa`. Language and flags come from the name
  (`Film.en.srt` → English, `Film.fr.forced.ass` → French (forced)).
* **Embedded text streams** (SubRip, ASS, mov_text): one VTT per stream.
* **Image subtitles** (PGS, VobSub) are listed in Media Information as not
  supported yet; burning them in is on the roadmap.

## 6. The page (`appcode/lizardking.py`)

* **Menu bar**: Media (Open File, Open Folder, Open Network Stream, Open
  Recent, Save Playlist), Playback (transport, jumps, Jump to Time, Next Frame,
  Speed, A→B Loop, Repeat, Shuffle), Audio (track, volume), Video (Full Screen,
  Fit, Zoom, Snapshot), Subtitles, View (Playlist panel, Dark/Light, Resume),
  Tools (Media Information), Help (shortcuts, About).
* **Player**: wapyt `MediaPlayer`, mode `auto` (audio items show their cover
  art). Items are queued **without a URL** and resolved when reached
  (`on_resolve` → `PlayerService.prepare`), so a folder of 500 files queues
  instantly and only the file being played is probed.
* **Playlist panel**: wapyt `Listbox`; double-click plays, × removes, filter.
* **Open dialog**: a modal with places (home, Videos, Music, Downloads,
  Desktop, mounted drives), a path field, and the folder's media. Multi-select;
  Play replaces the queue, Enqueue appends; a folder queues the media in it (and
  below, by default) in natural order; an `.m3u`/`.m3u8` queues its entries.
* **On-screen messages** for speed, volume, loop, fit, zoom and transcoding,
  and a "Resumed at 12:34 · Start over" banner.

### Keyboard

wapyt's player already handles Space/K, ←/→ (5 s), J/L (10 s), ↑/↓, M, F, N, P
and C when it has focus; LizardKing handles the same keys when it does not, and
adds:

| Key | Action | | Key | Action |
|---|---|---|---|---|
| S | Stop | | Shift+S | Snapshot |
| E | Next frame | | Shift+L | A→B loop |
| + ] / - [ | Faster / slower | | = | Normal speed |
| A | Cycle fit | | Z | Cycle zoom |
| B | Next audio track | | V | Next subtitle track |
| Ctrl+O / F / N | Open file / folder / stream | | Ctrl+T | Jump to time |
| Ctrl+I | Media information | | Ctrl+L | Playlist panel |
| Ctrl+Y | Save playlist | | ? | Shortcuts |

Where VLC's keys clash with wapyt's, wapyt's win: J/K/L already seek and
pause, so VLC's L (loop mode) becomes Shift+L for the A→B loop, and VLC's
J/K/G/H delay keys wait for a key plan (roadmap).

## 7. What is remembered (`store.py`)

`~/.local/share/lizardking/state.json` (or `$LIZARDKING_DATA`), replaced
atomically:

* **Recent**: the last 30 files, newest first.
* **Positions**: saved every 5 s, on pause, and when you switch away. Kept only
  between 15 s and the last 20 s (and 97%) of a file, so neither a glance nor
  the credits leave a resume point. At most 1000 files.
* **Settings**: volume, mute, theme, resume on/off, playlist panel, repeat,
  shuffle, last folder.

## 8. Security

LizardKing is a stand-alone app for one person: there is no login and no
access key. The server listens on **127.0.0.1 only**, so nothing on the network
can reach it; any account on the same machine can, which is accepted.

Media URLs carry tokens, not paths (§2). The Content-Security-Policy is
pyTincture's plus `media-src 'self' blob: http: https:` (hls.js plays through
`blob:` URLs) and `http: https:` in `connect-src` for network streams.

**DLNA.** The one thing LizardKing does on the network by itself: when you
open *Network (DLNA)* it sends an SSDP search (multicast, local network only)
and talks to the media servers that answer. It plays only URLs on a server it
discovered (`dlna.check_url`), refuses XML with a DTD, and serves a server's
files to the page through its own origin (`/lk/file/<token>`).

## 9. Testing

* `pytest`: the decision table, segment plans, the ffmpeg command line, a real
  transcode with a seek restart, playlists, sidecars, the listing, the store,
  and the two pyTincture rules that only fail at run time (module-level state
  in BFF files; annotations that refuse `null`).
* `tests/e2e/ui_check.py`: Chromium against a real server: opens without a key; a
  file from the command line; direct play with sidecar subtitles; jump, speed,
  fit, zoom, A→B loop, snapshot; an HEVC 10-bit + AC-3 Matroska transcoded, its
  full length, a far seek, switching audio track mid-film, Media Information; a
  WMA; Open Folder; Save Playlist; Recent + resume + Start over; the shortcuts;
  no CSP violations or page errors; encoders stopped at shutdown.
