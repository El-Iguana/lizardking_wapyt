# LizardKing roadmap

v0.1 is described in [docs/spec.md](docs/spec.md). Roughly in order of value:

## Playback

- **Copy-remux instead of re-encoding** when only the container or the audio is
  the problem (H.264 + DTS in .mkv, say). Needs segments cut on the source's own
  keyframes and their timestamps placed (Orpheus's `-segment_times` + `tfdt`
  rewrite), so a copied video stream costs no CPU and no quality.
- **HDR to SDR tonemapping** for transcodes (`zscale` + `tonemap`, measured in
  Orpheus at ~0.75× realtime in software).
- **Image subtitles** (PGS, VobSub) burned in during a transcode.
- **VA-API encoding** (Intel/AMD) alongside NVENC.
- **Network streams through ffmpeg**: RTSP, RTMP, and http media the browser
  cannot play (live streams have no duration, so an event playlist).
- **Force a transcode** from the menu, for a file the browser accepts but
  plays wrongly without reporting an error (as Chrome does with AC-3 in MKV:
  silence). The page already falls back by itself when an error *is* reported.
- **Chapters** from the container, in the seek bar and a menu.
- **Gapless audio** between album tracks.

## Controls

- Forced aspect ratios (16:9, 4:3, 2.35:1) and crop presets, as VLC's `A`/`C`.
- Subtitle delay and audio delay (VLC's G/H, J/K, which clash with wapyt's J/K:
  needs a key plan).
- Add a subtitle file to what is playing; subtitle size and position.
- Equalizer and audio normalisation (Web Audio).
- Video adjustments: brightness, contrast, saturation (CSS filters), deinterlace
  on transcode.
- Drag and drop of files from the desktop (the browser gives no paths: play them
  as blob URLs, direct only).
- Reorder the playlist by dragging.

## Media servers (DLNA)

- **Video posters** from servers that provide them (MiniDLNA gives album art
  only), and art as thumbnails in the Open dialog's list.
- More servers checked live: Plex, Jellyfin, Serviio (MiniDLNA 1.3.3 is).
- Play to a DLNA **renderer** (a TV): AVTransport, the other half of UPnP AV.

## Desktop

The Electron app (`desktop/`) has its own window, icon and menu entry, and
opens files from the file manager.

- A second launch with files should **hand them to the open window** (it only
  focuses it now).
- **Packaging**: an AppImage or Flatpak that bundles Electron, Python and
  ffmpeg, so it runs without a source checkout.
- A media key / MPRIS bridge on Linux (the page already sets Media Session
  metadata).
