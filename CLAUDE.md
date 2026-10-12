# LizardKing — pytincture + wapyt local media player

A VLC-style media player in the browser for local files: the page is Python in
Pyodide built from wapyt widgets; the server (FastAPI via pytincture) browses
the disk, probes with ffprobe, serves files directly with Range, and transcodes
what the browser cannot play to on-demand HLS with ffmpeg. Design and reasons:
`docs/spec.md`. Next: `ROADMAP.md`. Public repo: El-Iguana/lizardking_wapyt.

## Related repos

Siblings under `~/Development/Pytinc/`:

- `pytincture/` — the framework, pinned to tag `v1.0.0rc13` in `pyproject.toml`.
- `wa_pytincture_widgetset/` — **wapyt**. LizardKing uses `MediaPlayer`
  (incl. `refresh()`, from WAwesome-AI/wa_pytincture_widgetset#72),
  `MenuBar`, `Listbox`, `ModalWindow`. CI checks wapyt out at
  `[tool.lizardking] wapyt-ref` in `pyproject.toml`: the `v0.2.0` release
  commit. When moving it, run `uv lock` too (the lock records wapyt's version).
- `iguanaxterm_wapyt/`, `monguana_wapyt/` — sibling apps with the same
  structure; their CLAUDE.md files document more pytincture traps.
- The transcode design comes from Orpheus (`~/Development/ai_ecosystem/orpheus`,
  `server/internal/stream/`), a **private** repo: do not link it from anything
  public.

## Layout

```
service.py                 # ASGI entrypoint: create_app, media routes, static, CSP patch, CLI
appcode/                   # pytincture modules_path
  lizardking.py            #   the page (Pyodide). APP_ENTRYPOINT lives here.
  static/                  #   lk.js (caps, snapshot, fit/zoom), lizardking.css, logo (also the favicon) + poster — served at /lk/static
  services/
    player_service.py      #   BFF: browse, expand, prepare, info, positions, recent, settings, playlists
    routes.py              #   plain routes: /lk/file (+ DLNA proxy), /lk/hls, /lk/vtt, /lk/cover, /lk/art, /lk/thumb, /lk/health
    media.py               #   ffprobe (cached) + the direct/transcode decision — pure, tested
    transcode.py           #   HLS sessions: plan, playlist, ffmpeg args, restart-on-seek, reaper
    subtitles.py           #   sidecars + ffmpeg -f webvtt
    browse.py              #   places, folder listing, folder/playlist expansion
    dlna.py                #   SSDP discovery, ContentDirectory Browse, DIDL parsing; dlna:// dialog paths
    thumbs.py              #   video thumbnails for the Open dialog (one ffmpeg frame, disk cache)
    playlists.py           #   M3U read/write — pure
    store.py               #   state.json: recent, positions, settings
    tokens.py              #   path <-> random token for media URLs
    startup.py             #   files from the command line, handed to the first page
    config.py              #   paths, extensions, binaries, version
  wapyt-99.99.99-*.whl     #   dev wheel the BROWSER installs (git-ignored; dev_wheel.sh)
tests/                     # pytest (unit + one real transcode)
tests/e2e/ui_check.py      # Chromium end to end (makes its own media)
desktop/                   # Electron shell: main.js spawns service.py on a free port, one window, quit stops it
  install.sh               #   npm ci + Electron binary + ~/.local/share menu entry and icon
```

## Running

```bash
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode
uv run python service.py [FILES...] [--no-browser]   # opens http://127.0.0.1:8777/lizardking/
uv run --group dev pytest -q
uv run --with playwright python tests/e2e/ui_check.py   # CHROME=… optional; LIZARDKING_E2E_DIR for its temp dir
```

For a throwaway run, point `LIZARDKING_DATA` and `LIZARDKING_CACHE` at a
scratch directory so the real `~/.local/share/lizardking` is untouched.

| Variable | Default | What |
|---|---|---|
| `LIZARDKING_PORT` | `8777` | Port (always 127.0.0.1) |
| `LIZARDKING_ENCODER` | `auto` | NVENC if a test encode works, else libx264 |
| `LIZARDKING_DATA` | `~/.local/share/lizardking` | state.json |
| `LIZARDKING_CACHE` | `~/.cache/lizardking` | transcode sessions; wiped at start and stop |
| `LIZARDKING_FFMPEG` / `_FFPROBE` | from PATH | binaries |
| `LIZARDKING_LOG_LEVEL` | `warning` | uvicorn log level |

**After editing `../wa_pytincture_widgetset/wapyt/assets/*`**, rebuild the
dev wheel (command above) and restart. The browser installs the wheel in
`appcode/`, not the venv copy; the venv copy (non-editable) only lets
pytincture discover the widgetset.

### Desktop app (Electron)

`desktop/install.sh` installs it; the menu entry runs
`desktop/node_modules/electron/dist/electron desktop %F`. `main.js` prefers
`.venv/bin/python` (so SIGINT reaches service.py directly) and falls back to
`uv run`. Electron is pinned in `desktop/package.json`; newer npm skips its
install script, so `install.sh` runs `node_modules/electron/install.js` itself.
Electron plays H.264/AAC and VP9/Opus directly, not HEVC: the same decisions as
Chrome. To test headless: `xvfb-run` with `--remote-debugging-port` and attach
Playwright over CDP.

## Things that will bite

### BFF arguments are type-checked against their annotations

pytincture validates every JSON argument against the parameter annotation
*before* the call and answers **400** on a mismatch. `audio: int = None`
refuses the `null` the page sends, and the symptom is a player that just sits
there with an empty `src` (the 400 has no body by the time devtools looks).
Anything that may be None is `int | None` / `dict | None`.
`tests/test_bff.py` checks every BFF signature for this.

### A BFF module is re-executed on every call

So module-level state in `player_service.py` would be new each call.
Sessions, tokens, the store, the probe cache and startup files live in plain
modules. `tests/test_bff.py` loads the module with pytincture's own loader
twice and fails on module-level containers.

### No login, no access key

LizardKing is a stand-alone single-user app: it listens on 127.0.0.1 only and
asks for nothing. An access key (Jupyter-style) was tried and removed on
purpose; don't add authentication back.

### localhost redirects to 127.0.0.1

pytincture answers only the canonical host: `http://localhost:8777` got a bare
*Invalid host header* (400) that looks like a blank page. Its Origin checks are
pinned to `127.0.0.1`, so adding localhost to `allowed_hosts` is not enough;
`with_localhost_redirect` in `service.py` sends it on with a 307 instead.

### DLNA media servers

`dlna.py` is a plain module (server table and titles must outlive a BFF call).
The dialog sees `dlna://<udn>/<object id>` (both URL-quoted: MiniDLNA ids
contain `$`); `PlayerService.browse`/`expand` dispatch on that scheme and never
store it as `last_dir`. A DLNA listing also carries `label` (`Server › Video ›
Recently Added`, from container titles cached in `dlna._containers`), which the
path box shows; Go/Enter on an unchanged label reloads `state["path"]`, anything
typed is loaded as typed. *Media → Open Media Server* (Ctrl+D) is
`_open_dialog(start="dlna://")`.

Memory: `store.dlna_servers` keeps every server found (udn, name, control,
origin, description `location`; newest 20) in `state.json`. `dlna.discover`
starts the SSDP search in a thread (`_Search`, one at a time) and meanwhile
asks the remembered servers for their description (`_revive`: same UDN or it
does not count, so another device at that address is never trusted by
`check_url`). Any that answer: return at once, and the search adds new servers
to the table and the store when it ends. None: wait for the search (a server
whose address changed is found that way and its entry replaced). A server that
is off is not listed but stays remembered. `browse(path, show_all, resume)`:
the dialog's first load passes `resume=True`, and with `dlna://` that reopens
`settings["last_dlna"]` (the last server folder listed without error; the
server list itself is never stored), falling back to the server list when that
server does not answer. The *Network (DLNA)* place always lists servers.
Measured on the NAS: Open Media Server 2.15 s the first time, 0.05 s after.

Album art: `parse_didl` reads `upnp:albumArtURI` for containers and items, and
`children` gives a track without its own the art of the album container it is
in (known once the album list was browsed). `prepare` uses it as the poster
only when the file has no embedded picture (embedded covers are bigger:
MiniDLNA's are 160 px `JPEG_TN`), via `/lk/art/<token>`, which fetches it
(`dlna.check_url`), caches it, and serves **raster types only** (an SVG from
this origin could carry script). Music shows the poster large in the player
(`#lk-art`, `_show_art`), or the LizardKing poster when there is none (keep
that: the user likes it).

Video thumbnails in the DLNA browser: `PlayerService.browse` adds `thumb`
(`/lk/thumb/<token>`) to a server's video entries; the dialog's Listbox uses
`template="{rowHtml}"` and `browse_row()` builds each row (escaped) with a
lazy `<img>` for those. `thumbs.thumbnail` takes one frame a tenth of the way
in (max 300 s), 320 px JPEG, at most 2 ffmpeg at once, cached in
`cache_dir()/thumbs` (not wiped at start, unlike `transcode/`; keyed by URL,
size and duration so a rescanned server's reused URL gets a new frame; pruned
past 3000). No frame: the route answers the LizardKing avatar. Measured on
the NAS: first visible thumbnails ~1.3 s, cached ~0.7 s. A server's **item** is its plain http URL, and that is
the queue entry's `path`: `prepare` probes and transcodes it like a file
(`media.is_remote`: no `os.stat` cache key, `-reconnect` input options), and
direct play goes through `/lk/file/<token>`, which proxies the server with Range
passed through (same origin: seeking and snapshots work). `dlna.check_url`
refuses any URL whose origin is not a discovered server, so the page cannot
make the server fetch arbitrary addresses. Live-tested against MiniDLNA 1.3.3
(the NAS); the unit tests use canned XML.

### SIGTERM stops the encoders

uvicorn re-raises the signal that stopped it after shutting down; a default
SIGTERM then killed the process before `transcode.sessions.shutdown()`, leaving
ffmpeg running. `main()` turns SIGTERM into KeyboardInterrupt.

### CSP: hls.js needs `media-src blob:`

pytincture's policy has no `media-src`, so `default-src 'self'` blocks the
`blob:` URLs hls.js plays through: direct files play, transcodes do not.
`with_media_policy` adds `media-src 'self' blob: http: https:` and `http:
https:` to `connect-src` (network streams).

### Measured browser facts the decision table relies on

- Chrome plays H.264/AAC `.mkv` directly, but answers `maybe` to the bare MIME
  type: ask `canPlayType` **with codecs** (`lk.js`).
- **AC-3 in `.mkv` plays silently in Chrome** — picture, no sound, no error.
  Hence AC-3/E-AC-3 transcode unless `isTypeSupported` says yes.
- Ten-bit H.264 → transcode (black picture on hardware decoders).

### Transcoding

`transcode.py` follows Orpheus: server-written VOD playlist, segments rounded
to whole frames, forced keyframes, restart with `-ss`/`-start_number`/
`-output_ts_offset` (never `-copyts`), finished = listed in ffmpeg's event
playlist, `temp_file` for atomic rewrites, `yuv420p` always. The reaper stops
idle encoders but keeps the session so the URL stays valid.

### Page code

- `APP_ENTRYPOINT = "LizardKing"` is mandatory (pytincture's MainWindow
  detection only knows dhxpyt).
- Build UI in `load_ui()`, never `__init__`.
- The page lives at `/lizardking/`: use absolute URLs (`/lk/...`).
- An async step that can be overtaken (the Open dialog's first folder load vs a
  typed path) uses a serial number: only the latest result is applied.
- `body.dataset.ready / mode / resolved / loop` exist for the e2e test.

### Testing traps

- Chrome dies with *Socket path too long* if `TMPDIR` is a long path; the e2e
  takes `LIZARDKING_E2E_DIR` for its own files instead.
- A 40 s test film never gets a resume point (the last 20 s are excluded): the
  e2e film is two minutes.
- Wait for the *new* item (title + `dataset.resolved`) after opening a file;
  the previous one is still playing for a moment.

## Conventions

Public repo `El-Iguana/lizardking_wapyt`: work on a feature branch and open a PR,
like the other `*_wapyt` repos. Commit as `oldmangan <eliguana@protonmail.com>`.
