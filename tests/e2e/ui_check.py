"""
LizardKing end to end, in a real Chrome: the server, the page, ffmpeg.

    uv run --with playwright python tests/e2e/ui_check.py

(CHROME=/path/to/chrome to use a particular browser.) Needs ffmpeg with
libx264 and libx265, and the dev wheel in appcode/ (see README).

Makes its own media (ffmpeg), data and cache directories in a temporary
folder (LIZARDKING_E2E_DIR picks where; not TMPDIR, which Chrome also uses
for a socket whose path has a length limit), starts the server on port 8790 with a file on the command line, and
drives the page by keyboard and mouse as a person would. Prints PASS/FAIL per
check and exits non-zero if any failed. Not part of `pytest` (it needs a
browser and takes a minute or two).
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import pgs  # noqa: E402 - a hand-written PGS subtitle stream
PORT = int(os.getenv("LIZARDKING_TEST_PORT", "8790"))
BASE = f"http://127.0.0.1:{PORT}"
results = []


# The RMS level of each of the six channels the player decodes (front left,
# front right, centre, LFE, back left, back right), listened to for a second.
SPEAKER_LEVELS = """async () => {
    const ctx = new AudioContext(), source = ctx.createMediaElementSource(%s);
    const split = ctx.createChannelSplitter(6), quiet = ctx.createGain();
    quiet.gain.value = 0;
    quiet.connect(ctx.destination);
    source.connect(split);
    const meters = [0, 1, 2, 3, 4, 5].map(n => {
        const meter = ctx.createAnalyser();
        split.connect(meter, n);
        meter.connect(quiet);
        return meter;
    });
    await ctx.resume();
    await new Promise(done => setTimeout(done, 1000));
    return meters.map(meter => {
        const samples = new Float32Array(meter.fftSize);
        meter.getFloatTimeDomainData(samples);
        return Math.sqrt(samples.reduce((sum, x) => sum + x * x, 0) / samples.length);
    });
}"""


# The colour of the picture at a point given as fractions of its width and height.
PIXEL = """([x, y]) => {
    const v = %s, canvas = document.createElement("canvas");
    canvas.width = v.videoWidth;
    canvas.height = v.videoHeight;
    const pen = canvas.getContext("2d");
    pen.drawImage(v, 0, 0);
    return [...pen.getImageData(Math.round(x * v.videoWidth), Math.round(y * v.videoHeight), 1, 1).data].slice(0, 3);
}"""


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, stderr=subprocess.DEVNULL)


def make_media(media: Path):
    (media / "Album").mkdir(parents=True)
    # Two minutes: a position is only remembered away from the last 20 s.
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
           "-t", "120", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(media / "Direct Film.mp4"))
    (media / "Direct Film.en.srt").write_text("1\n00:00:01,000 --> 00:00:05,000\nHello from a sidecar\n\n"
                                             "2\n00:00:20,000 --> 00:00:25,000\nTwenty seconds in\n")
    (media / "emb.srt").write_text("1\n00:00:01,000 --> 00:01:00,000\nEmbedded English\n")
    # The main track is 5.1 with a tone in the back left speaker only, so the
    # page can tell six real channels from a stereo downmix.
    # Its picture is marked HDR (PQ, BT.2020), so a transcode tone maps it.
    # It also has a Blu-ray image subtitle (PGS): a magenta box low in the
    # picture for the first 58 s, which only shows if it is drawn into the video.
    (media / "emb.sup").write_bytes(pgs.sup([(0, 58, 760, 900, 400, 60)]))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24000/1001", "-f", "lavfi", "-i", "sine=frequency=330",
           "-f", "lavfi", "-i", "sine=frequency=660", "-i", str(media / "emb.srt"), "-i", str(media / "emb.sup"),
           "-t", "60", "-filter_complex", "[1:a]pan=5.1|BL=c0[main];"
           "[0:v]setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc[hdr]",
           "-map", "[hdr]", "-map", "[main]", "-map", "2", "-map", "3", "-map", "4",
           "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
           "-x265-params", "log-level=error", "-c:a", "ac3", "-c:s:0", "srt", "-c:s:1", "copy",
           "-metadata:s:s:1", "language=spa",
           "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=Main", "-metadata:s:a:1", "language=fra",
           "-metadata:s:s:0", "language=eng", str(media / "HEVC Film.mkv"))
    (media / "emb.srt").unlink()
    (media / "emb.sup").unlink()
    ffmpeg("-f", "lavfi", "-i", "color=c=orange:size=300x300", "-frames:v", "1", str(media / "cover.png"))
    for n in (1, 2, 3):
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency={200 * n}", "-i", str(media / "cover.png"), "-t", "20",
               "-map", "0", "-map", "1", "-c:a", "libmp3lame", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
               "-id3v2_version", "3", "-metadata", f"title=Song {n}", "-metadata", "artist=The Doors Test",
               "-metadata", "album=Album", str(media / "Album" / f"0{n} Song {n}.mp3"))
    (media / "cover.png").unlink()
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=500", "-t", "15", "-c:a", "wmav2", str(media / "Old.wma"))


def start_server(tmp: Path, *paths):
    env = {**os.environ, "LIZARDKING_PORT": str(PORT), "LIZARDKING_DATA": str(tmp / "data"),
           "LIZARDKING_CACHE": str(tmp / "cache"), "LIZARDKING_LOG_LEVEL": "warning"}
    log = open(tmp / "server.log", "a")
    proc = subprocess.Popen([sys.executable, str(ROOT / "service.py"), "--no-browser", *map(str, paths)],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(200):
        try:
            urllib.request.urlopen(f"{BASE}/lk/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    url = None
    for _ in range(50):
        found = re.search(r"LizardKing: (\S+)", (tmp / "server.log").read_text())
        if found:
            url = found.group(1)
            break
        time.sleep(0.1)
    return proc, url


def stop_server(proc):
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def status(path):
    try:
        return urllib.request.urlopen(BASE + path, timeout=10).status
    except urllib.error.HTTPError as error:
        return error.code


def main():
    tmp = Path(tempfile.mkdtemp(prefix="lizardking-e2e-", dir=os.getenv("LIZARDKING_E2E_DIR")))
    media = tmp / "media"
    make_media(media)
    proc, url = start_server(tmp, media / "Album" / "02 Song 2.mp3")
    try:
        run(tmp, media, url)
    finally:
        stop_server(proc)
    leftover = list((tmp / "cache" / "transcode").glob("*")) if (tmp / "cache" / "transcode").exists() else []
    check("stopping the server stops the encoders and empties the cache", not leftover, f"{len(leftover)} left")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


def run(tmp, media, url):
    check("the printed link is the plain page address", url == f"{BASE}/lizardking/", url)
    check("the page opens without any key", status("/lizardking/") == 200)
    landed = urllib.request.urlopen(f"http://localhost:{PORT}/lizardking/", timeout=10).url
    check("localhost is sent on to 127.0.0.1", landed == f"{BASE}/lizardking/", landed)
    check("a guessed media token is not a file", status("/lk/file/not-a-token") == 404)

    with sync_playwright() as p:
        # CHROME picks a browser; without it, Playwright's own Chromium
        # (`playwright install chromium`), which is what CI uses.
        browser = p.chromium.launch(executable_path=os.getenv("CHROME") or None,
                                    args=["--autoplay-policy=no-user-gesture-required"])
        context = browser.new_context(viewport={"width": 1400, "height": 860}, accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
        page.add_init_script("document.addEventListener('securitypolicyviolation', e => "
                             "(window.__csp = window.__csp || []).push(e.effectiveDirective + ' ' + e.blockedURI));")
        page.goto(url)
        page.wait_for_function("() => document.body.dataset.ready === 'true'", timeout=120000)
        video = "document.querySelector('#lk-media video')"

        def playing(after=0.5, timeout=60):
            page.wait_for_function(f"() => {{ const v = {video}; return v && !v.paused && v.readyState >= 3 && v.currentTime > {after}; }}",
                                   timeout=timeout * 1000)

        def now():
            return page.evaluate(f"() => {video}.currentTime")

        def body(key):
            return page.evaluate(f"() => document.body.dataset.{key} || ''")

        def press(keys):
            page.evaluate("() => document.activeElement && document.activeElement.blur()")
            page.keyboard.press(keys)

        def open_file(folder, name):
            press("Control+o")
            page.wait_for_selector("#lk-path")
            page.fill("#lk-path", str(folder))
            page.press("#lk-path", "Enter")
            item = page.locator(".wapyt-modal .wapyt-listbox-item", has_text=name).first
            try:
                item.wait_for(timeout=15000)
            except Exception:
                page.screenshot(path=str(tmp.parent / "lizardking-e2e-failure.png"))
                print("dialog shows:", page.evaluate("() => [document.querySelector('#lk-path').value, "
                      "[...document.querySelectorAll('.wapyt-modal .wapyt-listbox-item')].map(e => e.textContent)]"))
                raise
            item.dblclick()
            page.wait_for_selector(".wapyt-modal-overlay", state="detached")
            title = name.rsplit(".", 1)[0]
            page.wait_for_function(f"() => document.title.startsWith({title!r}) && document.body.dataset.resolved === {title!r}",
                                   timeout=30000)

        # 1. A file named on the command line plays when the page opens.
        playing(0.3)
        title = page.evaluate("() => document.title")
        check("a file given on the command line plays at start, titled from its tags", title.startswith("Song 2"), title)
        cover = page.evaluate(f"() => {video}.poster")
        check("an MP3 shows its embedded cover art", "/lk/cover/" in cover, cover[-40:])

        # 2. Open a file: plays directly, with its sidecar subtitles.
        open_file(media, "Direct Film.mp4")
        playing(0.5)
        state = page.evaluate(f"() => {{ const v = {video}; return {{src: v.currentSrc, tracks: [...v.textTracks].map(t => t.label)}}; }}")
        check("an H.264 MP4 plays directly from /lk/file", body("mode") == "direct" and "/lk/file/" in state["src"], state["src"][-50:])
        check("its .en.srt is offered as English subtitles", state["tracks"] == ["English"], f"{state['tracks']}")
        cue = page.evaluate(f"""() => new Promise(res => {{ const t = {video}.textTracks[0]; t.mode = 'hidden';
            setTimeout(() => res(t.cues ? [...t.cues].map(c => c.text) : null), 1500); }})""")
        check("the subtitles convert to WebVTT cues", cue == ["Hello from a sidecar", "Twenty seconds in"], f"{cue}")

        # 3. Jump to time, speed, fit, A-B loop, snapshot.
        press("Control+t")
        page.fill("#lk-jump", "0:20")
        page.press("#lk-jump", "Enter")
        page.wait_for_function(f"() => {video}.currentTime >= 20 && {video}.currentTime < 23", timeout=10000)
        check("Ctrl+T jumps to 0:20", True, f"{now():.1f}s")
        press("]")
        press("]")
        rate = page.evaluate(f"() => {video}.playbackRate")
        press("=")
        check("] twice is 1.5×, = back to normal", rate == 1.5 and page.evaluate(f"() => {video}.playbackRate") == 1, f"{rate}")
        press("a")
        fit = page.evaluate(f"() => {video}.style.objectFit")
        press("z")
        zoom = page.evaluate(f"() => {video}.style.transform")
        check("A crops to fill, Z zooms", fit == "cover" and zoom == "scale(1.25)", f"{fit}, {zoom}")
        press("Shift+L")
        page.wait_for_timeout(2200)
        press("Shift+L")
        loop = body("loop")
        a, b = (float(x) for x in loop.split("-"))
        page.wait_for_timeout(3500)
        t = now()
        check("Shift+L twice loops A→B", 1.5 < b - a < 3.5 and a - 0.5 <= t <= b + 0.6, f"loop {loop}, at {t:.1f}s")
        press("Shift+L")
        with page.expect_download(timeout=10000) as download:
            press("Shift+S")
        saved = download.value
        check("Shift+S saves a PNG snapshot", saved.suggested_filename.endswith(".png") and saved.suggested_filename.startswith("Direct Film"),
              saved.suggested_filename)

        # 4. A file the browser cannot play: HEVC 10-bit, AC-3, in Matroska.
        page.wait_for_timeout(5500)  # let a resume position be saved for the film
        open_file(media, "HEVC Film.mkv")
        playing(0.5, timeout=90)
        src = page.evaluate(f"() => {video}.currentSrc")
        check("HEVC 10-bit + AC-3 is transcoded to HLS and plays", body("mode") == "transcode" and src.startswith("blob:"), src[:30])
        duration = page.evaluate(f"() => {video}.duration")
        check("the transcode shows the film's whole length at once", abs(duration - 60) < 1, f"{duration:.2f}s")
        tracks = page.evaluate(f"() => [...{video}.textTracks].map(t => t.label)")
        check("its embedded subtitles are offered", tracks == ["English"], f"{tracks}")
        playing(3, timeout=60)
        levels = page.evaluate(SPEAKER_LEVELS % video)
        check("its 5.1 sound reaches the player as six channels, not a stereo mix",
              len(levels) == 6 and levels[4] > 0.05 and max(levels[:4] + levels[5:]) < levels[4] / 20,
              " ".join(f"{x:.3f}" for x in levels))
        press("Control+t")
        page.fill("#lk-jump", "0:48")
        started = time.time()
        page.press("#lk-jump", "Enter")
        playing(48.2, timeout=60)
        check("seeking far ahead restarts the encoder there and plays on", True, f"{time.time() - started:.1f}s to play at {now():.1f}s")
        before = now()
        press("b")
        page.wait_for_function(f"() => document.title && {video}.readyState >= 3 && !{video}.paused", timeout=60000)
        page.wait_for_timeout(2500)
        after = now()
        menu = page.evaluate("() => document.body.dataset.mode")
        check("B switches to the French track and carries on from the same place", abs(after - before) < 8 and menu == "transcode",
              f"{before:.1f}s → {after:.1f}s")
        press("Control+i")
        page.wait_for_selector("#lk-info-mode")
        info = page.inner_text(".wapyt-modal")
        check("Ctrl+I explains why it is transcoded", "Transcoded" in info and "HEVC" in info and "audio track 2 chosen" in info
              and "sound: AAC stereo" in info,
              re.sub(r"\s+", " ", info)[:120])
        page.keyboard.press("Escape")
        page.wait_for_selector(".wapyt-modal-overlay", state="detached")

        # 4b. An image subtitle: V steps past the text track to it, and it is in the picture.
        press("Control+t")
        page.fill("#lk-jump", "0:10")
        page.press("#lk-jump", "Enter")
        playing(10.2, timeout=60)

        def caption():
            """Whether the magenta box is where the subtitle puts it (the canvas scaled to 1280x720)."""
            page.wait_for_function(f"() => {video}.readyState >= 3 && !{video}.paused", timeout=60000)
            page.wait_for_timeout(2500)
            r, g, b = page.evaluate(PIXEL % video, [640 / 1280, 620 / 720])
            return r > 180 and g < 110 and b > 180, f"rgb({r}, {g}, {b}) at {now():.1f}s"

        for _ in range(4):
            if body("burn"):
                break
            press("v")
            page.wait_for_timeout(1500)
        shown, colour = caption()
        check("V reaches the image subtitle and it is drawn into the picture", body("burn") and shown, colour)
        press("Control+i")
        page.wait_for_selector("#lk-info-mode")
        info = page.inner_text(".wapyt-modal")
        check("Ctrl+I says the subtitle is why", "image subtitles drawn into the picture" in info, "")
        check("Ctrl+I says the HDR picture is tone mapped", "yes, tone mapped to SDR" in info, "")
        page.keyboard.press("Escape")
        page.wait_for_selector(".wapyt-modal-overlay", state="detached")
        press("v")
        page.wait_for_function("() => !document.body.dataset.burn", timeout=30000)
        shown, colour = caption()
        check("V again takes it out of the picture", not shown and 10 < now() < 40, colour)

        # 5. Audio the browser cannot play: transcoded, audio only.
        open_file(media, "Old.wma")
        playing(0.5, timeout=60)
        check("a WMA file is transcoded and plays", body("mode") == "transcode", "")

        # 6. A folder: its songs, in order, in the playlist.
        press("Control+f")
        page.wait_for_selector("#lk-path")
        page.fill("#lk-path", str(media / "Album"))
        page.press("#lk-path", "Enter")
        page.wait_for_timeout(800)
        page.locator(".wapyt-modal .lk-btn.lk-primary").click()
        page.wait_for_selector(".wapyt-modal-overlay", state="detached")
        page.wait_for_function("() => document.title.startsWith('Song 1')", timeout=30000)
        playing(0.3)
        rows = page.evaluate("() => [...document.querySelectorAll('#lk-queue-list .wapyt-listbox-title')].map(e => e.textContent)")
        check("Open Folder queues the album in order", rows == ["1. Song 1", "2. 02 Song 2", "3. 03 Song 3"], f"{rows}")
        press("n")
        page.wait_for_function("() => document.title.startsWith('Song 2')", timeout=20000)
        check("N plays the next song", True)

        # 7. Save the playlist and read it back.
        target = tmp / "saved.m3u8"
        press("Control+y")
        page.fill("#lk-save-path", str(target))
        page.press("#lk-save-path", "Enter")
        page.wait_for_selector(".wapyt-modal-overlay", state="detached")
        text = target.read_text() if target.exists() else ""
        check("Ctrl+Y saves an M3U with every song", text.startswith("#EXTM3U") and text.count(".mp3") == 3, f"{text.count('.mp3')} songs")

        # 8. Come back later: Recent, and resume where the film was left.
        page.reload()
        page.wait_for_function("() => document.body.dataset.ready === 'true'", timeout=120000)
        page.click(".wapyt-menubar-item:has-text('Media')")
        page.hover(".wapyt-menubar-menu [role=menuitem]:has-text('Open Recent')")
        page.locator(".wapyt-menubar-menu [role=menuitem]:has-text('Direct Film')").first.click()
        playing(0.5)
        resumed = now()
        banner = page.inner_text("#lk-resume")
        check("Recent reopens the film where it was left, and says so", resumed > 15 and "Resumed at" in banner, f"{resumed:.1f}s, '{banner.splitlines()[0]}'")
        page.click("#lk-resume [data-act=restart]")
        page.wait_for_function(f"() => {video}.currentTime < 5", timeout=10000)
        check("Start over goes back to the beginning", True)

        # 9. The help.
        press("?")
        page.wait_for_selector(".lk-keys")
        rows = page.locator(".lk-keys tr").count()
        page.keyboard.press("Escape")
        check("? lists the keyboard shortcuts", rows >= 20, f"{rows} rows")

        csp = page.evaluate("() => window.__csp || []")
        check("no Content-Security-Policy violations", not csp, f"{csp[:3]}")
        check("no uncaught page errors", not errors, f"{errors[:3]}")
        page.screenshot(path=str(tmp.parent / "lizardking-e2e.png"))
        browser.close()


if __name__ == "__main__":
    main()
