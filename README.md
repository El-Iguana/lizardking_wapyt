<p align="center"><img src="docs/images/lizardking.jpg" alt="LizardKing: an iguana in a fez and a white shirt, singing into a vintage microphone" width="320"></p>

# LizardKing

*I am the Lizard King. I can do anything.*

A local media player in the browser, in the spirit of VLC. Open any file on
your disk and it plays: what the browser can decode is served as it is, and
everything else (HEVC, ten-bit video, AC-3, DTS, WMA, AVI, …) is transcoded on
the fly by ffmpeg, with the full length shown and seeking anywhere.

Built with [pyTincture](https://github.com/pytincture/pytincture) (the page is
Python, running in Pyodide) and [wapyt](https://github.com/WAwesome-AI/wa_pytincture_widgetset)
(the widgets, including its MediaPlayer). Named for Jim Morrison, and for El Iguana.

- **Open** files, folders (with subfolders, in natural order) and M3U playlists
  from a dialog that browses your disk, or an http(s) / HLS address
- **Plays directly** when it can, **transcodes** when it must (NVENC if you have
  it, else libx264), and tells you which and why in *Media Information*
- **Subtitles**: `.srt` / `.ass` / `.vtt` beside the file, and text subtitles
  inside it
- **Audio tracks**: switch mid-film and carry on from the same place
- **Playlist** panel; save it as M3U; repeat and shuffle
- **Recent files** and **resume where you left off**
- Speed, jump to time, next frame, **A→B loop**, **snapshot**, fit and zoom,
  full screen, dark and light
- VLC's keyboard shortcuts where they fit (press `?`)

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- **ffmpeg and ffprobe** on your PATH
- A Chromium-based browser or Firefox
- Checkouts of [wapyt](https://github.com/WAwesome-AI/wa_pytincture_widgetset)
  beside this one (`../wa_pytincture_widgetset`), at the commit in
  `pyproject.toml` (`[tool.lizardking] wapyt-ref`)

## Run it

```bash
git clone https://github.com/WAwesome-AI/wa_pytincture_widgetset.git
git clone https://github.com/El-Iguana/lizardking_wapyt.git
cd lizardking_wapyt
git -C ../wa_pytincture_widgetset checkout "$(sed -n 's/^wapyt-ref = "\(.*\)"/\1/p' pyproject.toml)"
uv sync
../wa_pytincture_widgetset/scripts/dev_wheel.sh appcode   # the widgets the browser loads
uv run python service.py ~/Videos/some-film.mkv            # files or folders are optional
```

It opens `http://127.0.0.1:8777/lizardking/` in your browser. No login and no
key: it listens on this computer only (127.0.0.1), so nothing on your network
can reach it.

## Desktop app

LizardKing can also run as a desktop app: its own window and menu entry, no
browser. It uses [Electron](https://www.electronjs.org/), which starts the
server on a free port and stops it when you close the window. It needs Node.js
and the `uv sync` above.

```bash
desktop/install.sh      # Electron, the icon, and LizardKing in the application menu
```

Then start **LizardKing** from the menu, or run
`desktop/node_modules/electron/dist/electron desktop [FILES...]`. Run the
installer again if you move the checkout.

## Settings

| Variable | Default | |
|---|---|---|
| `LIZARDKING_PORT` | `8777` | Port on 127.0.0.1 |
| `LIZARDKING_ENCODER` | `auto` | `auto` (NVENC if it works, else libx264), or any ffmpeg H.264 encoder |
| `LIZARDKING_DATA` | `~/.local/share/lizardking` | Recent files, positions, settings |
| `LIZARDKING_CACHE` | `~/.cache/lizardking` | Transcode segments and subtitles; emptied at start |
| `LIZARDKING_FFMPEG` / `LIZARDKING_FFPROBE` | `ffmpeg` / `ffprobe` | Other binaries |
| `LIZARDKING_LOG_LEVEL` | `warning` | uvicorn's log level |

## Tests

```bash
uv run --group dev pytest -q                              # includes a real ffmpeg transcode
uv run --with playwright python tests/e2e/ui_check.py     # the whole app in Chromium
```

## More

- [docs/spec.md](docs/spec.md): how it works and why
- [ROADMAP.md](ROADMAP.md): what is next
- [CLAUDE.md](CLAUDE.md): notes for working on the code

MIT licence.
