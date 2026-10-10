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
  `[tool.lizardking] wapyt-ref` in `pyproject.toml` — **move it to #72's merge
  commit once that merges**.
- `iguanaxterm_wapyt/`, `monguana_wapyt/` — sibling apps with the same
  structure; their CLAUDE.md files document more pytincture traps.
- The transcode design comes from Orpheus (`~/Development/ai_ecosystem/orpheus`,
  `server/internal/stream/`), a **private** repo: do not link it from anything
  public.

## Layout

```
service.py                 # ASGI entrypoint: create_app, media routes, static, CSP patch, access key, CLI
appcode/                   # pytincture modules_path
  lizardking.py            #   the page (Pyodide). APP_ENTRYPOINT lives here.
  static/                  #   lk.js (caps, snapshot, fit/zoom), lizardking.css, logo (also the favicon) + poster — served at /lk/static
  services/
    player_service.py      #   BFF: browse, expand, prepare, info, positions, recent, settings, playlists
    routes.py              #   plain routes: /lk/file, /lk/hls, /lk/vtt, /lk/cover, /lk/health
    media.py               #   ffprobe (cached) + the direct/transcode decision — pure, tested
    transcode.py           #   HLS sessions: plan, playlist, ffmpeg args, restart-on-seek, reaper
    subtitles.py           #   sidecars + ffmpeg -f webvtt
    browse.py              #   places, folder listing, folder/playlist expansion
    playlists.py           #   M3U read/write — pure
    store.py               #   state.json: recent, positions, settings
    tokens.py              #   path <-> random token for media URLs
    startup.py             #   files from the command line, handed to the first page
    config.py              #   paths, extensions, binaries, version
  wapyt-99.99.99-*.whl     #   dev wheel the BROWSER installs (git-ignored; dev_wheel.sh)
tests/                     # pytest (unit + one real transcode)
tests/e2e/ui_check.py      # Chromium end to end (makes its own media)
```

## Running

```bash
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode
uv run python service.py [FILES...] [--no-browser]   # prints http://127.0.0.1:8777/lizardking/?key=…
uv run --group dev pytest -q
uv run --with playwright python tests/e2e/ui_check.py   # CHROME=… optional; LIZARDKING_E2E_DIR for its temp dir
```

For a throwaway run, point `LIZARDKING_DATA` and `LIZARDKING_CACHE` at a
scratch directory so the real `~/.local/share/lizardking` is untouched.

| Variable | Default | What |
|---|---|---|
| `LIZARDKING_PORT` | `8777` | Port (always 127.0.0.1) |
| `LIZARDKING_ENCODER` | `auto` | NVENC if a test encode works, else libx264 |
| `LIZARDKING_DATA` | `~/.local/share/lizardking` | state.json, access.key |
| `LIZARDKING_CACHE` | `~/.cache/lizardking` | transcode sessions; wiped at start and stop |
| `LIZARDKING_FFMPEG` / `_FFPROBE` | from PATH | binaries |
| `LIZARDKING_NO_KEY` | unset | `1` disables the access key |
| `LIZARDKING_LOG_LEVEL` | `warning` | uvicorn log level |

**After editing `../wa_pytincture_widgetset/wapyt/assets/*`**, rebuild the
dev wheel (command above) and restart. The browser installs the wheel in
`appcode/`, not the venv copy; the venv copy (non-editable) only lets
pytincture discover the widgetset.

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

### The access key gate and pytincture's own assets

`with_access_key` in `service.py` locks everything except `/lk/health`,
`/favicon.ico` and `/<app>/frontend/…`. pytincture's service worker and
preflight fetch the framework files with `credentials: "omit"`, so they never
carry the cookie; gating them breaks startup with *Failed to load stylesheet
…materialdesignicons.css*. Nothing of the app's is under those paths.

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
