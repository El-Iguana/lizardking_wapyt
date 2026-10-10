"""Playlists, subtitles beside a video, the Open dialog's listing, and what is remembered."""
import os

from services import browse, playlists, store, subtitles


def test_m3u_reads_what_other_players_write(tmp_path):
    text = ("﻿#EXTM3U\r\n#EXTINF:123,The Doors - Riders on the Storm\r\nmusic/riders.mp3\r\n"
            "file:///home/me/Videos/a%20film.mkv\r\n#EXTVLCOPT:network-caching=1000\r\n"
            "https://example.com/live.m3u8\r\n\r\n~/x.mp4\r\n")
    entries = playlists.parse_m3u(text, str(tmp_path))
    assert entries[0] == {"title": "The Doors - Riders on the Storm", "duration": 123.0,
                          "path": str(tmp_path / "music" / "riders.mp3")}
    assert entries[1]["path"] == "/home/me/Videos/a film.mkv" and entries[1]["title"] is None
    assert entries[2]["url"] == "https://example.com/live.m3u8"
    assert entries[3]["path"] == os.path.expanduser("~/x.mp4")


def test_m3u_written_reads_back():
    rows = [{"path": "/m/a.mp3", "title": "A", "duration": 61.7}, {"url": "https://x/y.m3u8", "title": "Live"},
            {"path": "/m/b.mp4"}, {"title": "nothing to play"}]
    text = playlists.write_m3u(rows)
    assert text.startswith("#EXTM3U\n#EXTINF:61,A\n/m/a.mp3\n")
    back = playlists.parse_m3u(text, "/")
    assert [e.get("path") or e.get("url") for e in back] == ["/m/a.mp3", "https://x/y.m3u8", "/m/b.mp4"]


def test_sidecar_subtitles_are_found_and_labelled(tmp_path):
    for name in ("Film.mkv", "Film.srt", "Film.en.srt", "Film.fr.forced.ass", "Film 2.srt", "Other.srt", "Film.idx", "Film.sub"):
        (tmp_path / name).write_text("x")
    found = {os.path.basename(s["path"]): s for s in subtitles.sidecars(str(tmp_path / "Film.mkv"))}
    assert set(found) == {"Film.srt", "Film.en.srt", "Film.fr.forced.ass", "Film 2.srt"}
    assert found["Film.en.srt"]["label"] == "English" and found["Film.en.srt"]["language"] == "en"
    assert found["Film.fr.forced.ass"]["label"] == "French (forced)"
    assert found["Film.srt"]["label"] == "Subtitles"


def test_listing_shows_folders_then_media_in_natural_order(tmp_path):
    for name in ("Episode 10.mkv", "Episode 2.mkv", "notes.txt", ".hidden.mp4", "song.flac"):
        (tmp_path / name).write_text("x")
    (tmp_path / "Season 1").mkdir()
    result = browse.listing(str(tmp_path))
    assert [e["name"] for e in result["entries"]] == ["Season 1", "Episode 2.mkv", "Episode 10.mkv", "song.flac"]
    assert result["parent"] == str(tmp_path.parent)
    everything = browse.listing(str(tmp_path), show_all=True)
    assert "notes.txt" in [e["name"] for e in everything["entries"]]


def test_a_missing_folder_falls_back_to_its_nearest_parent(tmp_path):
    assert browse.listing(str(tmp_path / "gone" / "deeper"))["path"] == str(tmp_path)


def test_expand_turns_folders_and_playlists_into_entries(tmp_path):
    album = tmp_path / "Album"
    (album / "Disc 2").mkdir(parents=True)
    for name in ("02 B.mp3", "01 A.mp3", "cover.jpg", "Disc 2/01 C.mp3"):
        (album / name).write_text("x")
    (tmp_path / "list.m3u").write_text("#EXTM3U\n#EXTINF:1,Named\nAlbum/01 A.mp3\nmissing.mp3\nhttps://s/x.m3u8\n")
    titles = [e["title"] for e in browse.expand([str(album)])]
    assert titles == ["01 A", "02 B", "01 C"]
    assert [e["title"] for e in browse.expand([str(album)], recursive=False)] == ["01 A", "02 B"]
    listed = browse.expand([str(tmp_path / "list.m3u")])
    assert [e.get("title") for e in listed] == ["Named", "https://s/x.m3u8"]
    assert listed[0]["kind"] == "audio" and listed[1]["url"] == "https://s/x.m3u8"


def test_positions_are_kept_only_in_the_middle(tmp_path):
    store.use_file(tmp_path / "state.json")
    store.set_position("/f.mkv", 600, 3600)
    assert store.position("/f.mkv") == 600
    store.set_position("/f.mkv", 5, 3600)          # back at the start: forgotten
    assert store.position("/f.mkv") is None
    store.set_position("/f.mkv", 3590, 3600)       # in the credits: forgotten
    assert store.position("/f.mkv") is None
    store.set_position("/f.mkv", 1200, None)       # no duration known: kept
    store.use_file(tmp_path / "state.json")        # survives a restart
    assert store.position("/f.mkv") == 1200


def test_recent_is_newest_first_without_duplicates(tmp_path):
    store.use_file(tmp_path / "state.json")
    for name in ("a", "b", "a"):
        store.add_recent(f"/{name}.mp4", name, "video")
    assert [r["title"] for r in store.recent()] == ["a", "b"]
    store.clear_recent()
    assert store.recent() == []


def test_settings_ignore_unknown_keys(tmp_path):
    store.use_file(tmp_path / "state.json")
    assert store.settings()["theme"] == "dark"
    result = store.update_settings({"theme": "light", "volume": 0.4, "evil": True})
    assert result["theme"] == "light" and result["volume"] == 0.4 and "evil" not in result


def test_places_list_each_drive_once(tmp_path, monkeypatch):
    real = tmp_path / "var" / "mnt"
    (real / "nas").mkdir(parents=True)
    (tmp_path / "mnt").symlink_to(real)
    original = browse.Path

    def remap(*parts):
        text = str(original(*parts))
        if text.startswith(("/var/mnt", "/mnt", "/run/media", "/media")):
            return original(str(tmp_path) + text)
        return original(*parts)
    monkeypatch.setattr(browse, "Path", remap)
    monkeypatch.setattr(browse.Path, "home", staticmethod(lambda: tmp_path / "home"), raising=False)
    labels = [p["label"] for p in browse.places()]
    assert labels.count("nas") == 1, labels
