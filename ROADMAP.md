# LizardKing roadmap

v0.1 is described in [docs/spec.md](docs/spec.md). The aim from here is
**reasonable parity with VLC** for playing local files and a NAS: what people
reach for in VLC every week should be in LizardKing, while the things VLC does
that do not fit a local, single-user player stay out (see the last section).

Already there: open files, folders, network addresses and DLNA servers; recent
files and M3U playlists; repeat and shuffle; speed, jump to time, next frame,
A→B loop, snapshot, fit and zoom, full screen; audio and subtitle tracks
(image subtitles drawn into the picture);
resume; Media Information; VLC's keys; and anything ffmpeg reads, transcoded on
the fly (5.1 surround kept, HDR tone mapped) when the player cannot decode it.

## 1. Next: gaps you hit in everyday use

The first three matter most for typical rips (HEVC, 5.1 E-AC-3 or DTS,
PGS or ASS subtitles, chapters):

1. **Styled ASS/SSA subtitles.** Converting them to WebVTT drops positioning,
   fonts and styling; VLC renders them with libass. A WebAssembly libass
   renderer (JASSUB-style) over the video would match it.
2. **Chapters** from the container, in the seek bar and a menu, with
   next/previous chapter keys.
3. **Subtitle delay and audio delay** (VLC's G/H, J/K, which clash with
   wapyt's J/K: needs a key plan).

Then:

- **Subtitle text encoding**: non-UTF-8 `.srt` files (Windows-1252, Latin-1,
  …) detected and converted (`-sub_charenc`), as VLC does.
- **Add a subtitle file** to what is playing; subtitle size and position.
- **Image subtitles, the rest**: files beside the video (`.sup`,
  `.sub`/`.idx`); forced image tracks switched on by themselves; the caption
  already showing when a seek lands inside it; a place in the player's CC
  menu; and drawing them over a directly played video so they need no
  transcode.
- **Choose the audio output device** (`setSinkId`).
- **7.1 kept as 7.1** in a transcode (it is mixed down to 5.1 now), if
  Chromium and Firefox both decode eight-channel AAC; a **stereo downmix**
  choice for when the player's own mix of a 5.1 track sounds wrong.
- **Durations up front** in the playlist: probe queued files in the background
  instead of when each one plays.
- **Forced aspect ratios** (16:9, 4:3, 2.35:1) and crop presets, as VLC's
  `A`/`C`.
- **Reorder the playlist by dragging**; **drag and drop** files from the
  desktop (in Electron, with their paths).
- **Force a transcode** from the menu, for a file the player accepts but plays
  wrongly without reporting an error (as Chrome does with AC-3 in MKV:
  silence). The page already falls back by itself when an error *is* reported.
- **Dolby Vision without an HDR10 layer** (profile 5) tone mapped too: it is
  not recognised as HDR now and keeps its wrong colours (libplacebo reads it).
- **Copy-remux instead of re-encoding** when only the container or the audio is
  the problem (H.264 + DTS in .mkv, say). Needs segments cut on the source's own
  keyframes and their timestamps placed (Orpheus's `-segment_times` + `tfdt`
  rewrite), so a copied video stream costs no CPU and no quality.
- **HEVC without a transcode**: find out whether Electron's Chromium decodes
  HEVC in hardware on Linux (VA-API / NVIDIA); if so, most NAS films would play
  directly.
- **VA-API encoding** (Intel/AMD) alongside NVENC.

## 2. Toward VLC parity

- **Discs**: DVD and Blu-ray without copy protection (ffmpeg's `dvdvideo` and
  `bluray` inputs: titles and chapters, no disc menus at first), and audio CDs.
- **Network streams through ffmpeg**: RTSP, RTMP, and http media the player
  cannot decode; **internet radio** (Icecast/Shoutcast). Live streams have no
  duration, so an event playlist and a live mode in the seek bar.
- **Convert / Save**: transcode a file to a chosen format and keep it
  (the engine is already here), and record what is playing.
- **Picture-in-picture, always on top, a mini player.**
- **Equalizer and audio normalisation** (Web Audio); a **visualizer** for
  music.
- **Video adjustments**: brightness, contrast, saturation (CSS filters);
  rotate and flip; deinterlace on transcode.
- **Gapless audio** between album tracks.
- **Bookmarks** in a file; a **sleep timer** and *quit at end of playlist*.
- **More playlist formats**: PLS and XSPF, read and written.
- **Statistics** in Media Information: bitrate, dropped and decoded frames,
  buffer.
- **Customisable keyboard shortcuts.**

## 3. Media servers (DLNA)

- **Video posters** from servers that provide them (MiniDLNA gives album art
  only); album art as the list's thumbnails for music.
- **Thumbnails for local videos** too (the DLNA browser has them).
- More servers checked live: Plex, Jellyfin, Serviio (MiniDLNA 1.3.3 is).
- Play to a DLNA **renderer** (a TV): AVTransport, the other half of UPnP AV.

## 4. Desktop

The Electron app (`desktop/`) has its own window, icon and menu entry, and
opens files from the file manager.

- A second launch with files should **hand them to the open window** (it only
  focuses it now).
- **Packaging**: an AppImage or Flatpak that bundles Electron, Python and
  ffmpeg, so it runs without a source checkout (VLC installs anywhere).
- A media key / **MPRIS** bridge on Linux (the page already sets Media Session
  metadata).

## Not planned

- **Streaming out** to other devices (VLC's *Stream*): LizardKing serves
  127.0.0.1 only, on purpose. Playing *to* a TV is the DLNA renderer item.
- **A media library** with scraped metadata: that is Orpheus's job.
- **Extensions, skins, 360° and 3D video.**
- **AC-3/DTS passthrough** to a receiver: a browser engine cannot send
  bitstreamed audio, so this is a known limit, not a gap.
