"""
LizardKing's page, in Python, run by Pyodide in the browser.

A menu bar, the player, and a playlist panel. Files are picked from the disk
through the server (``PlayerService``); the server says whether the browser
can play each one as it is or needs it transcoded, and hands back a URL either
way. Keyboard shortcuts follow VLC where wapyt's player does not already
define the key.
"""
import asyncio
import html
import time

import js
from pyodide.ffi import create_proxy

from wapyt import (
    CellConfig, LayoutConfig, ListAction, Listbox, ListboxConfig, MainWindow, MediaItem, MediaPlayer,
    MediaPlayerConfig, MediaTextTrack, MenuBarConfig, MenuItem, ModalConfig, ModalWindow,
)

from services.player_service import PlayerService

APP_ENTRYPOINT = "LizardKing"

# Browser tab icon. pytincture reads this literal from the app module and
# resolves it against modules_path; .webp is among the types it serves.
APP_FAVICON = "static/lizardking.webp"

SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0, 4.0]
FITS = [("contain", "Best Fit"), ("cover", "Crop to Fill"), ("fill", "Stretch")]
ZOOMS = [1.0, 1.25, 1.5, 2.0]
SAVE_EVERY = 5.0          # seconds between saved resume positions
DECODE_ERRORS = ("decode", "unsupported", "media")
ICONS = {"folder": "mdi-folder", "video": "mdi-movie-open-outline", "audio": "mdi-music-note",
         "playlist": "mdi-playlist-music", "subtitle": "mdi-subtitles-outline", "file": "mdi-file-outline",
         "server": "mdi-server-network", "image": "mdi-image-outline"}

SHORTCUTS = [
    ("Space, K", "Play / pause"), ("S", "Stop"), ("N / P", "Next / previous in the playlist"),
    ("← / →", "Back / forward 5 s"), ("J / L", "Back / forward 10 s"), ("E", "Next frame"),
    ("↑ / ↓", "Volume up / down"), ("M", "Mute"), ("+ ] / - [", "Faster / slower"), ("=", "Normal speed"),
    ("F", "Full screen (or double-click)"), ("A", "Cycle fit: best fit, crop, stretch"), ("Z", "Cycle zoom"),
    ("B", "Next audio track"), ("V", "Next subtitle track"), ("C", "Subtitles on / off"),
    ("Shift+L", "A→B loop: set A, set B, clear"), ("Shift+S", "Snapshot (PNG download)"),
    ("Ctrl+O", "Open file"), ("Ctrl+F", "Open folder"), ("Ctrl+D", "Open media server (DLNA)"),
    ("Ctrl+N", "Open network stream"),
    ("Ctrl+T", "Jump to time"), ("Ctrl+I", "Media information"), ("Ctrl+L", "Show / hide the playlist"),
    ("Ctrl+Y", "Save playlist"), ("?", "This list"),
]


def spawn(coro):
    """Run a coroutine from an event handler, and never lose its exception."""
    task = asyncio.ensure_future(coro)
    task.add_done_callback(lambda t: t.cancelled() or not t.exception() or js.console.error(repr(t.exception())))
    return task


def fmt_time(seconds):
    if seconds is None:
        return "--:--"
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text):
    """"90", "1:30", "1:02:03" or "1h2m3s" in seconds; None if it is none of those."""
    text = (text or "").strip().lower()
    if not text:
        return None
    try:
        if any(u in text for u in "hms") and ":" not in text:
            total, number = 0.0, ""
            for ch in text:
                if ch.isdigit() or ch == ".":
                    number += ch
                elif ch in "hms" and number:
                    total += float(number) * {"h": 3600, "m": 60, "s": 1}[ch]
                    number = ""
                elif not ch.isspace():
                    return None
            return total + (float(number) if number else 0)
        total = 0.0
        for part in text.split(":"):
            total = total * 60 + float(part or 0)
        return total
    except ValueError:
        return None


def browse_row(entry):
    """
    One Open-dialog row: the Listbox's own icon / title / meta layout, or, for a
    video with a thumbnail (a media server's), the frame beside the text. Built
    here with every value escaped; the Listbox inserts it as markup.
    """
    title = html.escape(entry["name"])
    meta = "" if entry["is_dir"] else html.escape(fmt_size(entry["size"]))
    icon = ICONS.get(entry["kind"], "mdi-file-outline")
    meta_line = f'<div class="wapyt-listbox-meta">{meta}</div>' if meta else ""
    if entry.get("thumb"):
        return (f'<div class="lk-row-thumb"><img class="lk-thumb" loading="lazy" decoding="async" alt="" '
                f'src="{html.escape(entry["thumb"])}"><div class="lk-row-text"><div class="wapyt-listbox-head">'
                f'<span class="wapyt-listbox-title">{title}</span></div>{meta_line}</div></div>')
    return (f'<div class="wapyt-listbox-head"><i class="wapyt-listbox-icon mdi {icon}" aria-hidden="true"></i>'
            f'<span class="wapyt-listbox-title">{title}</span></div>{meta_line}')


def fmt_size(size):
    if size is None:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def el(tag, cls=None, text=None, **attrs):
    node = js.document.createElement(tag)
    if cls:
        node.className = cls
    if text is not None:
        node.textContent = text
    for key, value in attrs.items():
        node.setAttribute(key.replace("_", "-"), str(value))
    return node


class Dialog:
    """A modal built for one use, whose DOM listeners go away with it."""

    def __init__(self, title, width=560, height=300):
        self.modal = ModalWindow(ModalConfig(title=title, width=width, height=height, dispose_on_close=True))
        self.body = el("div", "lk-dialog")
        self.modal.body.appendChild(self.body)
        self._proxies = []
        self.modal.on_close(lambda _e: self._release())

    def on(self, node, event, handler):
        proxy = create_proxy(handler)
        self._proxies.append(proxy)
        node.addEventListener(event, proxy)

    def button(self, parent, label, handler, primary=False):
        node = el("button", "lk-btn lk-primary" if primary else "lk-btn", label, type="button")
        self.on(node, "click", lambda _e: handler())
        parent.appendChild(node)
        return node

    def show(self):
        self.modal.show()

    def close(self):
        self.modal.close()

    def _release(self):
        for proxy in self._proxies:
            proxy.destroy()
        self._proxies = []


class LizardKing(MainWindow):
    # ── Building the page ─────────────────────────────────────────────────

    def load_ui(self):
        js.document.head.appendChild(el("link", rel="stylesheet", href="/lk/static/lizardking.css"))
        self.entries = []            # the queue, in the player's order: {uid, path | url, title, kind}
        self.options = {}            # uid -> {"audio": n, "force": bool, "burn": stream, "text": n (once)}
        self.prepared = {}           # uid -> what PlayerService.prepare returned
        self.caps = {}
        self.settings = {}
        self.recent_items = []
        self.fit, self.zoom = "contain", 1.0
        self.loop_a = self.loop_b = None
        self._uid = 0
        self._last_save = 0.0
        self._timers = {}
        self._proxies = []

        self.menubar = self.add_menubar("mainwindow_header", MenuBarConfig(items=self._menu_items(), label="LizardKing"))
        self.menubar.on_select(lambda e: self._command(e["id"]))
        self.menubar.on_open(lambda e: self._sync_checks())

        self.body = self.add_layout("mainwindow", LayoutConfig(cols=[
            CellConfig(id="player", grow=1),
            CellConfig(id="queue", header="Playlist", width="320px", collapsible=True, resizable=True),
        ]))
        self._build_player()
        self._build_queue()

        self._on(js.document, "keydown", self._key)
        self._on(js.window, "pagehide", lambda _e: self._save_position(force=True))
        script = el("script", src="/lk/static/lk.js")
        self._on(script, "load", lambda _e: spawn(self._boot()))
        js.document.head.appendChild(script)

    def _on(self, node, event, handler):
        proxy = create_proxy(handler)
        self._proxies.append(proxy)
        node.addEventListener(event, proxy)

    def _later(self, name, ms, fn):
        """Run fn after ms, replacing an earlier pending call of the same name."""
        old = self._timers.pop(name, None)
        if old:
            js.clearTimeout(old[0])
            old[1].destroy()

        def fire(*_):
            entry = self._timers.pop(name, None)
            try:
                fn()
            finally:
                if entry:
                    entry[1].destroy()
        proxy = create_proxy(fire)
        self._timers[name] = (js.setTimeout(proxy, ms), proxy)

    def _build_player(self):
        self.body.attach_html("player", (
            '<div class="lk-player" id="lk-player">'
            '<div id="lk-media" style="height:100%"></div>'
            '<div class="lk-empty" id="lk-empty"><img src="/lk/static/lizardking-poster.webp" alt="">'
            '<b>LizardKing</b><span>Open a file with <kbd>Ctrl</kbd>+<kbd>O</kbd>, '
            'a folder with <kbd>Ctrl</kbd>+<kbd>F</kbd>, a media server with <kbd>Ctrl</kbd>+<kbd>D</kbd></span><span class="lk-muted">Press <kbd>?</kbd> for shortcuts</span></div>'
            '<div class="lk-art lk-hidden" id="lk-art"><img alt=""><b></b><span></span></div>'
            '<div class="lk-osd" id="lk-osd" role="status" aria-live="polite"></div>'
            '<div class="lk-resume" id="lk-resume"><span></span>'
            '<button type="button" data-act="restart">Start over</button>'
            '<button type="button" data-act="dismiss" aria-label="Dismiss">×</button></div>'
            '</div>'))
        self.player = MediaPlayer(MediaPlayerConfig(
            mode="auto", autoplay=True, keyboard=True, time_update_interval=0.25,
            empty_text="", aria_label="Player", height="100%",
        ), container=js.document.getElementById("lk-media"))
        self.player.on_resolve(self._resolve)
        self.player.on_change(self._changed)
        self.player.on_time_update(self._time)
        self.player.on_pause(lambda _e: self._save_position(force=True))
        self.player.on_ended(self._ended)
        self.player.on_error(self._error)
        self.player.on_volume(self._volume)
        self.player.on_tracks(lambda _e: self._rebuild_menus())
        self._on(js.document.getElementById("lk-resume"), "click", self._resume_click)

    def _build_queue(self):
        self.body.attach_html("queue", (
            '<div class="lk-queue"><div class="lk-queue-bar"><span id="lk-queue-count">Empty</span></div>'
            '<div class="lk-queue-list" id="lk-queue-list"></div></div>'))
        bar = js.document.querySelector(".lk-queue-bar")
        for label, handler in (("Save", self._save_playlist_dialog), ("Clear", self._clear)):
            node = el("button", "lk-btn", label, type="button")
            node.style.padding = "2px 10px"
            self._on(node, "click", lambda _e, h=handler: h())
            bar.appendChild(node)
        self.queue = Listbox(ListboxConfig(
            selection="single", filterable=True, filter_placeholder="Filter the playlist",
            empty_text="Nothing queued. Media → Open File…", label="Playlist",
            actions=[ListAction("remove", "Remove", "mdi-close")],
        ), container=js.document.getElementById("lk-queue-list"))
        self.queue.on_activate(lambda e: self._play_uid(e["id"]))
        self.queue.on_action(lambda e: e["action"] == "remove" and self._remove(e["id"]))

    async def _boot(self):
        self.caps = js.lizardking.caps().to_py()
        service = PlayerService()
        self.settings = await service.settings_async()
        self.set_theme(self.settings.get("theme") or "dark")
        self.player.set_volume(self.settings.get("volume", 1.0))
        self.player.set_muted(bool(self.settings.get("muted")))
        self.player.set_repeat(self.settings.get("repeat") or "off")
        self.player.set_shuffle(bool(self.settings.get("shuffle")))
        if not self.settings.get("playlist_open", True):
            self.body.hide("queue")
        self.recent_items = await service.recent_async()
        self._rebuild_menus()
        pending = await service.startup_async()
        if pending:
            self._enqueue(pending, replace=True)
        js.document.body.dataset.ready = "true"

    # ── Menus ─────────────────────────────────────────────────────────────

    def _menu_items(self):
        state = self._state()
        recent = [MenuItem(f"recent:{n}", r["title"], ICONS.get(r.get("kind"), "mdi-file-outline"))
                  for n, r in enumerate(self.recent_items[:15])] if hasattr(self, "recent_items") else []
        if recent:
            recent += [MenuItem(separator=True), MenuItem("clear_recent", "Clear Recent")]
        else:
            recent = [MenuItem("no_recent", "Nothing yet", disabled=True)]
        audio_items = [MenuItem(f"audio:{t['index']}", t["label"], group="audio", checked=t["index"] == self._current_audio())
                       for t in self._audio_tracks()] or [MenuItem("no_audio", "No audio tracks", disabled=True)]
        text = (state.get("tracks") or {}).get("text") or []
        showing = next((i for i, t in enumerate(text) if t.get("showing")), -1)
        burn = self._current_burn()
        sub_items = [MenuItem("sub:-1", "Off", group="sub", checked=showing == -1 and burn is None)]
        sub_items += [MenuItem(f"sub:{i}", t.get("label") or f"Track {i + 1}", group="sub",
                               checked=i == showing and burn is None) for i, t in enumerate(text)]
        sub_items += [MenuItem(f"burn:{t['index']}", t["label"], group="sub", checked=t["index"] == burn)
                      for t in self._image_subs()]
        rate = state.get("rate") or 1.0
        settings = getattr(self, "settings", {})
        fit, zoom = getattr(self, "fit", "contain"), getattr(self, "zoom", 1.0)
        return [
            MenuItem("media", "Media", items=[
                MenuItem("open_file", "Open File…", "mdi-file-video-outline", shortcut="Ctrl+O"),
                MenuItem("open_folder", "Open Folder…", "mdi-folder-open-outline", shortcut="Ctrl+F"),
                MenuItem("open_dlna", "Open Media Server (DLNA)…", "mdi-server-network", shortcut="Ctrl+D"),
                MenuItem("open_url", "Open Network Stream…", "mdi-web", shortcut="Ctrl+N"),
                MenuItem("recent", "Open Recent", "mdi-history", items=recent),
                MenuItem(separator=True),
                MenuItem("save_playlist", "Save Playlist…", "mdi-content-save-outline", shortcut="Ctrl+Y"),
            ]),
            MenuItem("playback", "Playback", items=[
                MenuItem("toggle", "Play / Pause", "mdi-play-pause", shortcut="Space"),
                MenuItem("stop", "Stop", "mdi-stop", shortcut="S"),
                MenuItem("previous", "Previous", "mdi-skip-previous", shortcut="P"),
                MenuItem("next", "Next", "mdi-skip-next", shortcut="N"),
                MenuItem(separator=True),
                MenuItem("back10", "Jump Backward 10 s", "mdi-rewind-10", shortcut="J"),
                MenuItem("fwd10", "Jump Forward 10 s", "mdi-fast-forward-10", shortcut="L"),
                MenuItem("jump", "Jump to Time…", "mdi-timer-outline", shortcut="Ctrl+T"),
                MenuItem("frame", "Next Frame", "mdi-step-forward", shortcut="E"),
                MenuItem(separator=True),
                MenuItem("speed", "Speed", "mdi-speedometer", items=[
                    MenuItem(f"rate:{r}", "Normal" if r == 1 else f"{r:g}×", group="rate", checked=abs(r - rate) < 1e-6)
                    for r in SPEEDS]),
                MenuItem("ab", self._ab_label(), "mdi-repeat-variant", shortcut="Shift+L"),
                MenuItem(separator=True),
                MenuItem("repeat", "Repeat", "mdi-repeat", items=[
                    MenuItem("repeat:off", "Off", group="repeat", checked=state.get("repeat", "off") == "off"),
                    MenuItem("repeat:all", "All", group="repeat", checked=state.get("repeat") == "all"),
                    MenuItem("repeat:one", "One", group="repeat", checked=state.get("repeat") == "one"),
                ]),
                MenuItem("shuffle", "Shuffle", "mdi-shuffle-variant", checkable=True, checked=bool(state.get("shuffle"))),
            ]),
            MenuItem("audio", "Audio", items=[
                MenuItem("audio_track", "Audio Track", "mdi-music-box-multiple-outline", items=audio_items),
                MenuItem(separator=True),
                MenuItem("vol_up", "Increase Volume", "mdi-volume-plus", shortcut="↑"),
                MenuItem("vol_down", "Decrease Volume", "mdi-volume-minus", shortcut="↓"),
                MenuItem("mute", "Mute", "mdi-volume-off", shortcut="M"),
            ]),
            MenuItem("video", "Video", items=[
                MenuItem("fullscreen", "Full Screen", "mdi-fullscreen", shortcut="F"),
                MenuItem("fit", "Fit", "mdi-aspect-ratio", items=[
                    MenuItem(f"fit:{key}", label, group="fit", checked=key == fit) for key, label in FITS]),
                MenuItem("zoom", "Zoom", "mdi-magnify-plus-outline", items=[
                    MenuItem(f"zoom:{z}", f"{z:g}×", group="zoom", checked=abs(z - zoom) < 1e-6) for z in ZOOMS]),
                MenuItem(separator=True),
                MenuItem("snapshot", "Take Snapshot", "mdi-camera-outline", shortcut="Shift+S"),
            ]),
            MenuItem("subtitles", "Subtitles", items=[
                MenuItem("sub_track", "Subtitle Track", "mdi-subtitles-outline", items=sub_items),
            ]),
            MenuItem("view", "View", items=[
                MenuItem("playlist", "Playlist", "mdi-playlist-play", checkable=True,
                         checked=bool(settings.get("playlist_open", True)), shortcut="Ctrl+L"),
                MenuItem(separator=True),
                MenuItem("theme:dark", "Dark", group="theme", checked=settings.get("theme", "dark") == "dark"),
                MenuItem("theme:light", "Light", group="theme", checked=settings.get("theme") == "light"),
                MenuItem(separator=True),
                MenuItem("resume", "Resume Where I Left Off", checkable=True, checked=bool(settings.get("resume", True))),
            ]),
            MenuItem("tools", "Tools", items=[
                MenuItem("info", "Media Information", "mdi-information-outline", shortcut="Ctrl+I"),
            ]),
            MenuItem("help", "Help", items=[
                MenuItem("keys", "Keyboard Shortcuts", "mdi-keyboard-outline", shortcut="?"),
                MenuItem("about", "About LizardKing", "mdi-information-variant"),
            ]),
        ]

    def _rebuild_menus(self):
        if not self.menubar.is_open():
            self.menubar.set_items(self._menu_items())

    def _sync_checks(self):
        """Marks follow what the player's own buttons changed (shuffle, repeat, speed)."""
        state = self._state()
        self.menubar.set_checked("shuffle", bool(state.get("shuffle")))
        self.menubar.set_checked(f"repeat:{state.get('repeat', 'off')}", True)
        rate = state.get("rate") or 1.0
        for r in SPEEDS:
            if abs(r - rate) < 1e-6:
                self.menubar.set_checked(f"rate:{r}", True)

    def _command(self, cid):
        simple = {
            "open_file": lambda: spawn(self._open_dialog()),
            "open_folder": lambda: spawn(self._open_dialog(folder=True)),
            "open_dlna": lambda: spawn(self._open_dialog(start="dlna://")),
            "open_url": self._url_dialog, "save_playlist": self._save_playlist_dialog,
            "toggle": self.player.toggle, "stop": self._stop, "previous": self.player.previous, "next": self.player.next,
            "back10": lambda: self.player.skip(-10), "fwd10": lambda: self.player.skip(10),
            "jump": self._jump_dialog, "frame": self._next_frame, "ab": self._ab,
            "vol_up": lambda: self._nudge_volume(0.05), "vol_down": lambda: self._nudge_volume(-0.05),
            "mute": lambda: self.player.set_muted(not self._state().get("muted")),
            "fullscreen": lambda: self.player.fullscreen(), "snapshot": self._snapshot,
            "playlist": self._toggle_playlist, "info": lambda: spawn(self._info_dialog()),
            "keys": self._keys_dialog, "about": lambda: spawn(self._about_dialog()),
            "clear_recent": lambda: spawn(self._clear_recent()),
            "shuffle": lambda: self._set_setting("shuffle", not self._state().get("shuffle"), self.player.set_shuffle),
            "resume": lambda: self._set_setting("resume", not self.settings.get("resume", True)),
        }
        if cid in simple:
            simple[cid]()
            return
        kind, _, value = cid.partition(":")
        if kind == "recent":
            entry = self.recent_items[int(value)]
            self._enqueue([{"path": entry["path"], "title": entry["title"], "kind": entry.get("kind") or "video"}], replace=True)
        elif kind == "rate":
            self._set_rate(float(value))
        elif kind == "repeat":
            self._set_setting("repeat", value, self.player.set_repeat)
        elif kind == "audio":
            self._choose_audio(int(value))
        elif kind == "sub":
            self._choose_subtitle(text=int(value))
        elif kind == "burn":
            self._choose_subtitle(burn=int(value))
        elif kind == "fit":
            self.fit = value
            self._apply_picture()
        elif kind == "zoom":
            self.zoom = float(value)
            self._apply_picture()
        elif kind == "theme":
            self._set_setting("theme", value, self.set_theme)

    def _set_setting(self, key, value, apply=None):
        if apply:
            apply(value)
        self.settings[key] = value
        spawn(PlayerService().update_settings_async({key: value}))
        self._rebuild_menus()

    # ── The queue ─────────────────────────────────────────────────────────

    def _enqueue(self, entries, replace=False):
        if not entries:
            self.osd("Nothing playable there")
            return
        fresh = []
        for entry in entries:
            self._uid += 1
            fresh.append({**entry, "uid": f"q{self._uid}"})
        items = [self._item(e) for e in fresh]
        if replace or not self.entries:
            self._save_position(force=True)
            self.entries = fresh
            self.player.load(items, 0, autoplay=True)
        else:
            self.entries += fresh
            self.player.add(items)
            self.osd(f"Added {len(fresh)} to the playlist" if len(fresh) != 1 else f"Added {fresh[0]['title']}")
        self._render_queue()

    def _item(self, entry):
        if entry.get("url"):
            return MediaItem(src=entry["url"], title=entry["title"], kind="video", data={"uid": entry["uid"]})
        return MediaItem(title=entry["title"], kind=entry.get("kind") or "video", data={"uid": entry["uid"]})

    def _render_queue(self):
        current = self._current()
        items = []
        for n, e in enumerate(self.entries):
            p = self.prepared.get(e["uid"]) or {}
            playing = current is not None and current["uid"] == e["uid"]
            items.append({"id": e["uid"], "title": f"{n + 1}. {e['title']}", "subtitle": p.get("subtitle") or "",
                          "meta": fmt_time(p["duration"]) if p.get("duration") else "",
                          "icon": "mdi-play" if playing else ICONS.get(e.get("kind"), "mdi-web")})
        self.queue.set_items(items)
        if current:
            self.queue.select(current["uid"])
        total = sum((self.prepared.get(e["uid"]) or {}).get("duration") or 0 for e in self.entries)
        count = len(self.entries)
        label = "Empty" if not count else f"{count} item{'s' if count != 1 else ''}"
        if total:
            label += f" · {fmt_time(total)}"
        js.document.getElementById("lk-queue-count").textContent = label

    def _index_of(self, uid):
        return next((n for n, e in enumerate(self.entries) if e["uid"] == uid), -1)

    def _play_uid(self, uid):
        n = self._index_of(uid)
        if n >= 0:
            self.player.play_index(n)

    def _remove(self, uid):
        n = self._index_of(uid)
        if n < 0:
            return
        self.player.remove(n)
        del self.entries[n]
        self.prepared.pop(uid, None)
        self._render_queue()
        if not self.entries:
            self._set_title(None)

    def _clear(self):
        self._save_position(force=True)
        self.player.clear()
        self.entries, self.prepared = [], {}
        self._render_queue()
        self._set_title(None)

    def _current(self):
        n = self._state().get("index", -1)
        return self.entries[n] if n is not None and 0 <= n < len(self.entries) else None

    def _state(self):
        try:
            return self.player.state() if hasattr(self, "player") else {}
        except Exception:  # noqa: BLE001 - before the player exists
            return {}

    # ── Player events ─────────────────────────────────────────────────────

    async def _resolve(self, payload):
        uid = (payload.get("data") or {}).get("uid")
        n = self._index_of(uid)
        if n < 0:
            raise ValueError("This item is no longer in the playlist.")
        entry = self.entries[n]
        opts = self.options.get(uid, {})
        r = await PlayerService().prepare_async(entry["path"], self.caps, opts.get("audio"), bool(opts.get("force")),
                                                opts.get("burn"))
        # The text track a subtitle switch asked for (-1: none), once; else the
        # file's own defaults, unless an image subtitle is in the picture.
        show = opts.pop("text", None)

        def shown(n, track):
            if show is not None:
                return n == show
            return bool(track.get("default")) and r.get("burn") is None
        self.prepared[uid] = r
        entry["title"] = r["title"]
        current = self._current()
        if current and current["uid"] == uid:
            self._set_title(r["title"])
        self._render_queue()
        spawn(self._refresh_recent())
        result = {
            "src": r["src"], "title": r["title"], "subtitle": r.get("subtitle") or None, "kind": r["kind"],
            "poster": r.get("poster"),
            "text_tracks": [MediaTextTrack(t["src"], label=t.get("label"), language=t.get("language"),
                                           default=shown(n, t)) for n, t in enumerate(r["text_tracks"])],
        }
        if current and current["uid"] == uid:
            self._show_art(r)
        js.document.body.dataset.mode = r["mode"]
        js.document.body.dataset.burn = "" if r.get("burn") is None else str(r["burn"])
        js.document.body.dataset.resolved = entry["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if not payload.get("refresh"):
            result["start"] = r.get("start") or 0
            if r.get("start"):
                self._show_resume(r["start"])
            if r["mode"] == "transcode":
                self.osd("Transcoding: " + "; ".join(r["reasons"]), 4000)
        return result

    def _changed(self, payload):
        self.loop_a = self.loop_b = None
        current = self._current()
        self._set_title(current["title"] if current else None)
        self._render_queue()
        self._apply_picture()
        self._rebuild_menus()

    def _time(self, payload):
        position, duration = payload.get("position") or 0, payload.get("duration")
        if self.loop_a is not None and self.loop_b is not None and position >= self.loop_b:
            self.player.seek(self.loop_a)
        self._save_position(position=position, duration=duration)

    def _ended(self, payload):
        current = self._current()
        if current and current.get("path"):
            duration = payload.get("duration") or 0
            spawn(PlayerService().save_position_async(current["path"], duration, duration))

    def _save_position(self, force=False, position=None, duration=None):
        now = time.monotonic()
        if not force and now - self._last_save < SAVE_EVERY:
            return
        current = self._current()
        if not current or not current.get("path") or current["uid"] not in self.prepared:
            return
        if position is None:
            state = self._state()
            position, duration = state.get("position"), state.get("duration")
        if position is None:
            return
        self._last_save = now
        spawn(PlayerService().save_position_async(current["path"], position, duration))

    def _error(self, payload):
        current = self._current()
        if not current or not current.get("path"):
            return
        opts = self.options.setdefault(current["uid"], {})
        prepared = self.prepared.get(current["uid"]) or {}
        if payload.get("code") in DECODE_ERRORS and prepared.get("mode") == "direct" and not opts.get("force"):
            # The browser said it could play this, then could not: ask for a transcode.
            opts["force"] = True
            self.osd("The browser could not decode this directly; transcoding", 4000)
            self.player.refresh()

    def _volume(self, payload):
        values = {"volume": payload.get("volume", 1.0), "muted": bool(payload.get("muted"))}
        self._later("volume", 600, lambda: spawn(PlayerService().update_settings_async(values)))

    # ── Commands ──────────────────────────────────────────────────────────

    def _set_title(self, title):
        js.document.title = f"{title} — LizardKing" if title else "LizardKing"
        js.document.getElementById("lk-empty").classList.toggle("lk-hidden", bool(self.entries))
        if not self.entries:
            self._show_art(None)

    def _show_art(self, prepared):
        """Music fills the player with its cover (the file's, or the media server's)."""
        box = js.document.getElementById("lk-art")
        if not prepared or prepared.get("kind") != "audio":
            box.classList.add("lk-hidden")
            return
        box.querySelector("img").src = prepared.get("poster") or "/lk/static/lizardking-poster.webp"
        box.querySelector("b").textContent = prepared.get("title") or ""
        box.querySelector("span").textContent = prepared.get("subtitle") or ""
        box.classList.remove("lk-hidden")

    def _stop(self):
        self._save_position(force=True)
        self.player.stop()

    def _set_rate(self, rate):
        self.player.set_rate(rate)
        self.osd(f"Speed {rate:g}×")

    def _step_rate(self, direction):
        rate = self._state().get("rate") or 1.0
        if direction == 0:
            self._set_rate(1.0)
            return
        options = [r for r in SPEEDS if (r > rate + 1e-6 if direction > 0 else r < rate - 1e-6)]
        if options:
            self._set_rate(options[0] if direction > 0 else options[-1])

    def _nudge_volume(self, delta):
        volume = max(0.0, min(1.0, (self._state().get("volume") or 0) + delta))
        self.player.set_volume(volume)
        self.osd(f"Volume {round(volume * 100)}%")

    def _video(self):
        return js.document.querySelector("#lk-media video")

    def _apply_picture(self):
        js.lizardking.picture(self._video(), self.fit, self.zoom)

    def _cycle_fit(self):
        keys = [k for k, _ in FITS]
        self.fit = keys[(keys.index(self.fit) + 1) % len(keys)]
        self._apply_picture()
        self.osd(dict(FITS)[self.fit])
        self._rebuild_menus()

    def _cycle_zoom(self):
        self.zoom = ZOOMS[(ZOOMS.index(self.zoom) + 1) % len(ZOOMS)] if self.zoom in ZOOMS else 1.0
        self._apply_picture()
        self.osd(f"Zoom {self.zoom:g}×")
        self._rebuild_menus()

    def _ab_label(self):
        a, b = getattr(self, "loop_a", None), getattr(self, "loop_b", None)
        if a is None:
            return "A→B Loop: Set A"
        if b is None:
            return f"A→B Loop: Set B (A = {fmt_time(a)})"
        return f"A→B Loop: Clear ({fmt_time(a)}–{fmt_time(b)})"

    def _ab(self):
        position = self._state().get("position") or 0
        if self.loop_a is None:
            self.loop_a = position
            self.osd(f"Loop A at {fmt_time(position)}")
        elif self.loop_b is None:
            if position <= self.loop_a + 0.5:
                self.osd("B has to be after A")
                return
            self.loop_b = position
            self.player.seek(self.loop_a)
            self.osd(f"Looping {fmt_time(self.loop_a)} – {fmt_time(self.loop_b)}")
        else:
            self.loop_a = self.loop_b = None
            self.osd("Loop off")
        js.document.body.dataset.loop = "" if self.loop_b is None else f"{self.loop_a:.2f}-{self.loop_b:.2f}"
        self._rebuild_menus()

    def _next_frame(self):
        current = self._current()
        fps = (self.prepared.get(current["uid"]) or {}).get("fps") if current else 0
        state = self._state()
        if not state.get("paused"):
            self.player.pause()
        self.player.seek((state.get("position") or 0) + 1.0 / (fps or 25))

    def _snapshot(self):
        current = self._current()
        name = f"{(current or {}).get('title') or 'snapshot'} {fmt_time(self._state().get('position')).replace(':', '-')}.png"
        self.osd("Snapshot saved to your downloads" if js.lizardking.snapshot(self._video(), name) else "Nothing to take a snapshot of")

    def _audio_tracks(self):
        current = self._current() if hasattr(self, "entries") else None
        return (self.prepared.get(current["uid"]) or {}).get("audio_tracks", []) if current else []

    def _current_audio(self):
        current = self._current() if hasattr(self, "entries") else None
        return (self.prepared.get(current["uid"]) or {}).get("audio") if current else None

    def _choose_audio(self, index):
        current = self._current()
        if not current or index == self._current_audio():
            return
        self.options.setdefault(current["uid"], {})["audio"] = index
        label = next((t["label"] for t in self._audio_tracks() if t["index"] == index), f"Track {index + 1}")
        self.osd(f"Audio: {label}")
        self.player.refresh()

    def _cycle_audio(self):
        tracks = self._audio_tracks()
        if len(tracks) < 2:
            self.osd("Only one audio track" if tracks else "No audio")
            return
        self._choose_audio(((self._current_audio() or 0) + 1) % len(tracks))

    def _image_subs(self):
        current = self._current() if hasattr(self, "entries") else None
        return (self.prepared.get(current["uid"]) or {}).get("image_subtitles", []) if current else []

    def _current_burn(self):
        current = self._current() if hasattr(self, "entries") else None
        return (self.prepared.get(current["uid"]) or {}).get("burn") if current else None

    def _choose_subtitle(self, text=-1, burn=None):
        """Show text track ``text`` (-1: none) or image subtitle ``burn`` (its stream), never both."""
        current = self._current()
        if not current or (burn is not None and burn == self._current_burn()):
            return
        if burn is None and self._current_burn() is None:
            self.player.set_text_track(None if text < 0 else text)
            return
        # An image subtitle is part of the picture: putting one in or taking
        # it out is another transcode, carried on from the same moment.
        opts = self.options.setdefault(current["uid"], {})
        opts["burn"], opts["text"] = burn, -1 if burn is not None else text
        self.player.refresh()

    def _cycle_subtitles(self):
        """Off → each text track → each image subtitle → off."""
        text = (self._state().get("tracks") or {}).get("text") or []
        images = self._image_subs()
        if not text and not images:
            self.osd("No subtitles")
            return
        burn = self._current_burn()
        if burn is not None:
            at = len(text) + next((i for i, t in enumerate(images) if t["index"] == burn), 0)
        else:
            at = next((i for i, t in enumerate(text) if t.get("showing")), -1)
        nxt = at + 1
        if nxt < len(text):
            self._choose_subtitle(text=nxt)
            self.osd(f"Subtitles: {text[nxt].get('label') or nxt + 1}")
        elif nxt < len(text) + len(images):
            self._choose_subtitle(burn=images[nxt - len(text)]["index"])
            self.osd(f"Subtitles: {images[nxt - len(text)]['label']}")
        else:
            self._choose_subtitle()
            self.osd("Subtitles off")
        self._rebuild_menus()

    def _toggle_subtitles(self):
        text = (self._state().get("tracks") or {}).get("text") or []
        images = self._image_subs()
        if self._current_burn() is not None or any(t.get("showing") for t in text):
            self._choose_subtitle()
        elif text:
            self._choose_subtitle(text=0)
        elif images:
            self._choose_subtitle(burn=images[0]["index"])

    def _toggle_playlist(self):
        shown = not self.settings.get("playlist_open", True)
        (self.body.show if shown else self.body.hide)("queue")
        self._set_setting("playlist_open", shown)

    async def _refresh_recent(self):
        self.recent_items = await PlayerService().recent_async()
        self._rebuild_menus()

    async def _clear_recent(self):
        await PlayerService().clear_recent_async()
        await self._refresh_recent()

    # ── On-screen messages ────────────────────────────────────────────────

    def osd(self, text, ms=1600):
        node = js.document.getElementById("lk-osd")
        node.textContent = text
        node.classList.add("lk-on")
        self._later("osd", ms, lambda: js.document.getElementById("lk-osd").classList.remove("lk-on"))

    def _show_resume(self, position):
        node = js.document.getElementById("lk-resume")
        node.querySelector("span").textContent = f"Resumed at {fmt_time(position)}"
        node.classList.add("lk-on")
        self._later("resume", 8000, lambda: js.document.getElementById("lk-resume").classList.remove("lk-on"))

    def _resume_click(self, event):
        act = event.target.getAttribute("data-act")
        if act == "restart":
            self.player.seek(0)
        if act:
            js.document.getElementById("lk-resume").classList.remove("lk-on")

    # ── Keyboard ──────────────────────────────────────────────────────────

    def _key(self, event):
        if event.defaultPrevented or event.isComposing:
            return
        target = event.target
        tag = (target.tagName or "").upper() if target and hasattr(target, "tagName") else ""
        if tag in ("INPUT", "TEXTAREA", "SELECT") or (target and getattr(target, "isContentEditable", False)):
            return
        if js.document.querySelector(".wapyt-modal-overlay"):
            return
        closest = (lambda sel: bool(target.closest(sel))) if target and hasattr(target, "closest") else (lambda sel: False)
        in_player, in_widget = closest(".wapyt-media"), closest(".wapyt-listbox, .wapyt-menubar")
        key, ctrl, shift = event.key, event.ctrlKey or event.metaKey, event.shiftKey
        action = None
        if event.altKey:
            return
        if ctrl:
            action = {"o": "open_file", "f": "open_folder", "d": "open_dlna", "n": "open_url", "t": "jump", "i": "info",
                      "l": "playlist", "y": "save_playlist"}.get(key.lower())
        elif shift and key in ("S", "L"):
            action = "snapshot" if key == "S" else "ab"
        else:
            action = {"s": "stop", "a": "_fit", "z": "_zoom", "b": "_audio", "v": "_subs", "e": "frame",
                      "+": "_faster", "]": "_faster", "-": "_slower", "[": "_slower", "=": "_normal", "?": "keys"}.get(key)
            if action is None and not in_player and not in_widget:
                # The player's own keys, for when it does not have the focus.
                action = {" ": "toggle", "k": "toggle", "ArrowLeft": "_back5", "ArrowRight": "_fwd5",
                          "j": "back10", "l": "fwd10", "ArrowUp": "vol_up", "ArrowDown": "vol_down",
                          "m": "mute", "f": "fullscreen", "n": "next", "p": "previous", "c": "_subs_toggle"}.get(key)
        if not action:
            return
        event.preventDefault()
        local = {
            "_fit": self._cycle_fit, "_zoom": self._cycle_zoom, "_audio": self._cycle_audio, "_subs": self._cycle_subtitles,
            "_faster": lambda: self._step_rate(1), "_slower": lambda: self._step_rate(-1), "_normal": lambda: self._step_rate(0),
            "_back5": lambda: self.player.skip(-5), "_fwd5": lambda: self.player.skip(5),
            "_subs_toggle": self._toggle_subtitles,
        }
        (local.get(action) or (lambda: self._command(action)))()

    # ── Dialogs ───────────────────────────────────────────────────────────

    async def _open_dialog(self, folder=False, start=""):
        d = Dialog("Open Folder" if folder else "Open Media", width=780, height=580)
        state = {"path": "", "parent": None}
        places = el("div", "lk-places")
        path_row = el("div", "lk-row")
        up = d.button(path_row, "↑ Up", lambda: spawn(load(state["parent"])))
        path_input = el("input", type="text", spellcheck="false", aria_label="Folder", id="lk-path")
        path_row.appendChild(path_input)
        d.button(path_row, "Go", lambda: spawn(load(typed())))
        error = el("div", "lk-error")
        browse = el("div", "lk-browse")
        options = el("div", "lk-row lk-muted")
        show_all = el("input", type="checkbox", id="lk-show-all")
        options.appendChild(show_all)
        options.appendChild(el("label", None, "Show all files", **{"for": "lk-show-all"}))
        recursive = el("input", type="checkbox", id="lk-recursive")
        recursive.checked = True
        options.appendChild(recursive)
        options.appendChild(el("label", None, "Include subfolders", **{"for": "lk-recursive"}))
        actions = el("div", "lk-actions")
        actions.appendChild(el("span", "lk-grow lk-muted", "Double-click a folder to open it, a file to play it."))
        for node in (places, path_row, error, browse, options, actions):
            d.body.appendChild(node)
        listbox = Listbox(ListboxConfig(selection="multi", filterable=True, filter_placeholder="Filter",
                                        empty_text="No media in this folder", label="Files",
                                        template="{rowHtml}"), container=browse)

        def go(replace):
            paths = listbox.get_selected_ids() or [state["path"]]
            d.close()
            spawn(self._open_paths(paths, replace, bool(recursive.checked)))

        d.button(actions, "Cancel", d.close)
        d.button(actions, "Enqueue", lambda: go(False))
        d.button(actions, "Play Folder" if folder else "Play", lambda: go(True), primary=True)

        async def load(path, resume=False):
            if path is None:
                return
            # Only the latest request counts: the first load (the last folder
            # used) can come back after a path typed in the meantime.
            state["serial"] = serial = state.get("serial", 0) + 1
            shown = path_input.value
            r = await PlayerService().browse_async(path, bool(show_all.checked), resume)
            if serial != state["serial"]:
                return
            # A media server's folders read as "Server › Video › Recently
            # Added"; the dlna:// path behind them stays in state.
            state["path"], state["parent"] = r["path"], r["parent"]
            state["label"] = r.get("label") or r["path"]
            # Unless the box was typed into while this folder loaded: then
            # leave it, so Enter opens what was typed rather than this folder.
            if path_input.value == shown:
                path_input.value = state["label"]
            up.disabled = not r["parent"]
            error.textContent = r.get("error") or ""
            entries = [e for e in r["entries"] if e["is_dir"]] if folder else r["entries"]
            listbox.set_items([{"id": e["path"], "title": e["name"], "is_dir": e["is_dir"], "rowHtml": browse_row(e)}
                               for e in entries])

        def typed():
            """The path in the box; the folder shown, if it still holds that folder's label."""
            value = path_input.value.strip()
            return state["path"] if value == state.get("label") else value

        def activate(e):
            item = e.get("item") or {}
            if item.get("is_dir"):
                spawn(load(e["id"]))
            elif not folder:
                d.close()
                spawn(self._open_paths([e["id"]], True, True))

        listbox.on_activate(activate)
        d.on(path_input, "keydown", lambda e: e.key == "Enter" and spawn(load(typed())))
        d.on(show_all, "change", lambda _e: spawn(load(state["path"])))
        d.modal.on_close(lambda _e: listbox.destroy())
        d.show()
        # The start folder (the last one used, on the disk or on a media
        # server) first, so its serial is the lowest: a path typed while the
        # places are still loading always wins.
        spawn(load(start, True))
        for place in await PlayerService().places_async():
            d.button(places, place["label"], lambda p=place["path"]: spawn(load(p))).title = place["path"]

    async def _open_paths(self, paths, replace=True, recursive=True):
        self._enqueue(await PlayerService().expand_async(paths, recursive), replace=replace)

    def _url_dialog(self):
        d = Dialog("Open Network Stream", width=600, height=230)
        d.body.appendChild(el("div", "lk-muted", "An http(s) address of a file or HLS (.m3u8) stream the browser can play."))
        url = el("input", type="url", placeholder="https://example.com/stream.m3u8", spellcheck="false", aria_label="Address")
        d.body.appendChild(url)
        actions = el("div", "lk-actions")
        d.body.appendChild(actions)

        def go(replace):
            value = url.value.strip()
            if not value.lower().startswith(("http://", "https://")):
                url.focus()
                return
            d.close()
            title = value.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or value
            self._enqueue([{"url": value, "title": title, "kind": "video"}], replace=replace)

        d.button(actions, "Cancel", d.close)
        d.button(actions, "Enqueue", lambda: go(False))
        d.button(actions, "Play", lambda: go(True), primary=True)
        d.on(url, "keydown", lambda e: e.key == "Enter" and go(True))
        d.show()
        url.focus()

    def _jump_dialog(self):
        state = self._state()
        d = Dialog("Jump to Time", width=380, height=210)
        row = el("div", "lk-row")
        field = el("input", type="text", spellcheck="false", aria_label="Time", id="lk-jump")
        field.value = fmt_time(state.get("position") or 0)
        row.appendChild(field)
        row.appendChild(el("span", "lk-muted", f"of {fmt_time(state.get('duration'))}"))
        d.body.appendChild(row)
        error = el("div", "lk-error")
        d.body.appendChild(error)
        actions = el("div", "lk-actions")
        d.body.appendChild(actions)

        def go():
            seconds = parse_time(field.value)
            if seconds is None:
                error.textContent = "Write it as 1:23:45, 83:45, 5025 or 1h23m45s."
                return
            d.close()
            self.player.seek(seconds)
            self.osd(f"Jumped to {fmt_time(seconds)}")

        d.button(actions, "Cancel", d.close)
        d.button(actions, "Jump", go, primary=True)
        d.on(field, "keydown", lambda e: e.key == "Enter" and go())
        d.show()
        field.focus()
        field.select()

    def _save_playlist_dialog(self):
        if not self.entries:
            self.osd("The playlist is empty")
            return
        d = Dialog("Save Playlist", width=600, height=230)
        field = el("input", type="text", spellcheck="false", aria_label="File", id="lk-save-path")
        field.value = f"{(self.settings.get('last_dir') or '~').rstrip('/')}/playlist.m3u8"
        d.body.appendChild(el("div", "lk-muted", "Extended M3U, which VLC and mpv read too."))
        d.body.appendChild(field)
        error = el("div", "lk-error")
        d.body.appendChild(error)
        actions = el("div", "lk-actions")
        d.body.appendChild(actions)

        async def save():
            rows = [{"path": e.get("path"), "url": e.get("url"), "title": e["title"],
                     "duration": (self.prepared.get(e["uid"]) or {}).get("duration")} for e in self.entries]
            try:
                r = await PlayerService().save_playlist_async(field.value, rows)
            except Exception as exc:  # noqa: BLE001 - shown in the dialog
                error.textContent = str(exc).strip().splitlines()[-1]
                return
            d.close()
            self.osd(f"Saved {r['count']} items to {r['path']}", 3000)

        d.button(actions, "Cancel", d.close)
        d.button(actions, "Save", lambda: spawn(save()), primary=True)
        d.on(field, "keydown", lambda e: e.key == "Enter" and spawn(save()))
        d.show()
        field.focus()

    async def _info_dialog(self):
        current = self._current()
        if not current or not current.get("path"):
            self.osd("Media information is for local files")
            return
        opts = self.options.get(current["uid"]) or {}
        r = await PlayerService().info_async(current["path"], self.caps, opts.get("audio"), opts.get("burn"))
        info, plan = r["info"], r["plan"]
        e = html.escape

        def rows(pairs):
            return "".join(f"<tr><th>{e(str(k))}</th><td>{e(str(v))}</td></tr>" for k, v in pairs if v not in (None, ""))

        label = {"direct": "Plays directly", "transcode": "Transcoded"}.get(plan["mode"], "Not playable")
        out = [f'<p><span class="lk-badge lk-{plan["mode"]}" id="lk-info-mode">{label}</span></p>']
        if plan["reasons"]:
            out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in plan["reasons"]) + "</ul>")
        if r.get("encoder"):
            sound = {6: " · sound: AAC 5.1", 2: " · sound: AAC stereo"}.get(r.get("audio_channels"), "")
            out.append(f"<p class='lk-muted'>Encoder: {e(r['encoder'])}{sound}</p>")
        tags = info.get("tags") or {}
        out.append("<h3>File</h3><table>" + rows([
            ("Name", r["name"]), ("Folder", r["path"].rsplit("/", 1)[0]), ("Container", info.get("format_long")),
            ("Duration", fmt_time(info.get("duration"))), ("Size", fmt_size(info.get("size"))),
            ("Bit rate", f"{info['bit_rate'] // 1000} kb/s" if info.get("bit_rate") else None),
            *[(k.replace("_", " ").title(), v) for k, v in tags.items()],
        ]) + "</table>")
        for v in info.get("video", []):
            fps = f"{v['fps']:.3f}".rstrip("0").rstrip(".") + " fps" if v.get("fps") else None
            out.append(f"<h3>Video · stream {v['index']}{' (cover art)' if v.get('attached_pic') else ''}</h3><table>" + rows([
                ("Codec", v.get("codec_long") or v.get("codec")), ("Profile", v.get("profile")),
                ("Resolution", f"{v.get('width')}×{v.get('height')}" if v.get("width") else None),
                ("Frame rate", fps), ("Pixel format", v.get("pix_fmt")), ("HDR", ("yes, tone mapped to SDR" if r.get("tone_mapped") else "yes") if v.get("hdr") else None),
            ]) + "</table>")
        for n, a in enumerate(info.get("audio", [])):
            out.append(f"<h3>Audio {n + 1} · stream {a['index']}{' (default)' if a.get('default') else ''}</h3><table>" + rows([
                ("Codec", a.get("codec_long") or a.get("codec")), ("Language", a.get("language")), ("Title", a.get("title")),
                ("Channels", a.get("channel_layout") or a.get("channels")),
                ("Sample rate", f"{a['sample_rate']} Hz" if a.get("sample_rate") else None),
            ]) + "</table>")
        for s in info.get("subtitles", []):
            out.append(f"<h3>Subtitles · stream {s['index']}</h3><table>" + rows([
                ("Codec", s.get("codec_long") or s.get("codec")), ("Language", s.get("language")), ("Title", s.get("title")),
                ("Shown", "as text" if s.get("text") else "drawn into the picture (a transcode)" if s.get("image")
                 else "no: this kind of subtitle is not supported"),
            ]) + "</table>")
        d = Dialog(f"Media Information — {r['name']}", width=640, height=560)
        body = el("div", "lk-info")
        body.innerHTML = "".join(out)
        d.body.appendChild(body)
        d.show()

    def _keys_dialog(self):
        d = Dialog("Keyboard Shortcuts", width=560, height=620)
        body = el("div", "lk-info")

        def keys_html(keys):
            return " ".join(html.escape(k) if k == "/" else f"<kbd>{html.escape(k)}</kbd>" for k in keys.split(" "))
        body.innerHTML = "<table class='lk-keys'>" + "".join(
            f"<tr><td>{keys_html(keys)}</td><td>{html.escape(what)}</td></tr>" for keys, what in SHORTCUTS) + "</table>"
        d.body.appendChild(body)
        d.show()

    async def _about_dialog(self):
        r = await PlayerService().about_async()
        d = Dialog("About LizardKing", width=460, height=440)
        body = el("div", "lk-info")
        body.innerHTML = (
            "<div style='text-align:center'><img src='/lk/static/lizardking.webp' alt='' width='128' height='128' style='border-radius:50%'>"
            f"<h2 style='margin:8px 0 2px'>LizardKing {html.escape(r['version'])}</h2>"
            "<p class='lk-muted' style='margin:0 0 12px'>“I am the Lizard King. I can do anything.”</p>"
            "<p>A local media player in the browser, built on pyTincture and wapyt. "
            "What the browser can play, it plays as it is; ffmpeg transcodes the rest.</p>"
            f"<p>Transcoding with <b>{html.escape(r['encoder'])}</b></p>"
            f"<p><a href='{html.escape(r['repo'])}' target='_blank' rel='noopener'>{html.escape(r['repo'])}</a></p></div>")
        d.body.appendChild(body)
        d.show()
