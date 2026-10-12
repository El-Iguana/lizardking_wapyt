"""DLNA: SSDP answers, device descriptions, DIDL-Lite, and the dialog's view of a server."""
import pytest

from services import dlna, media

DEVICE = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>NAS</friendlyName>
    <UDN>uuid:abc-123</UDN>
    <serviceList>
      <service><serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType><controlURL>/ctl/ConnectionMgr</controlURL></service>
      <service><serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType><controlURL>/ctl/ContentDir</controlURL></service>
    </serviceList>
  </device>
</root>"""

DIDL = """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
<container id="1$7" parentID="1" childCount="59"><dc:title>Album</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>
<item id="1$4$0" parentID="1$4"><dc:title>Ants Marching</dc:title><upnp:class>object.item.audioItem.musicTrack</upnp:class>
 <upnp:artist>Dave Matthews Band</upnp:artist><upnp:album>Under the Table</upnp:album>
 <res size="2048" duration="0:06:01.920" protocolInfo="http-get:*:audio/x-flac:*">http://10.0.0.2:8200/MediaItems/558.flac</res></item>
<item id="2$8$0" parentID="2$8"><dc:title>A Film</dc:title><upnp:class>object.item.videoItem</upnp:class>
 <res protocolInfo="http-get:*:video/x-matroska:*" duration="1:50:28.788">http://10.0.0.2:8200/MediaItems/49.mkv</res></item>
<item id="3$1" parentID="3"><dc:title>A Photo</dc:title><upnp:class>object.item.imageItem.photo</upnp:class>
 <res protocolInfo="http-get:*:image/jpeg:*">http://10.0.0.2:8200/MediaItems/9.jpg</res></item>
<item id="9" parentID="3"><dc:title>No address</dc:title><upnp:class>object.item.videoItem</upnp:class></item>
</DIDL-Lite>"""


def test_an_ssdp_answer_gives_its_location():
    answer = (b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1810\r\nLOCATION: http://10.0.0.2:8200/rootDesc.xml\r\n"
              b"ST: urn:schemas-upnp-org:device:MediaServer:1\r\nSERVER: MiniDLNA/1.3.3\r\n\r\n")
    assert dlna.parse_ssdp(answer)["location"] == "http://10.0.0.2:8200/rootDesc.xml"
    assert dlna.parse_ssdp(b"M-SEARCH * HTTP/1.1\r\n\r\n") is None
    assert dlna.parse_ssdp(b"HTTP/1.1 200 OK\r\nLOCATION: file:///etc/passwd\r\n\r\n") is None


def test_a_device_description_gives_the_content_directory():
    server = dlna.parse_device(DEVICE, "http://10.0.0.2:8200/rootDesc.xml")
    assert server == {"udn": "uuid:abc-123", "name": "NAS", "control": "http://10.0.0.2:8200/ctl/ContentDir",
                      "origin": "http://10.0.0.2:8200", "location": "http://10.0.0.2:8200/rootDesc.xml"}


def test_xml_with_a_dtd_is_refused():
    with pytest.raises(dlna.DlnaError):
        dlna.parse_didl('<!DOCTYPE x [<!ENTITY a "aaaa">]><DIDL-Lite>&a;</DIDL-Lite>')


def test_didl_gives_containers_and_playable_items_in_order():
    containers, items = dlna.parse_didl(DIDL)
    assert containers == [{"id": "1$7", "parent": "1", "title": "Album", "count": 59, "art": ""}]
    assert [i["title"] for i in items] == ["Ants Marching", "A Film", "A Photo"]   # no <res>: skipped
    song, film, photo = items
    assert (song["kind"], song["url"], song["size"], song["artist"], song["album"]) == (
        "audio", "http://10.0.0.2:8200/MediaItems/558.flac", 2048, "Dave Matthews Band", "Under the Table")
    assert song["duration"] == pytest.approx(361.92)
    assert (film["kind"], film["mime"], film["size"]) == ("video", "video/x-matroska", None)
    assert photo["kind"] == "image"


def test_paths_survive_the_ids_servers_use():
    path = dlna.make_path("uuid:abc-123", "1$4$0")
    assert path.startswith("dlna://") and "$" not in path
    assert dlna.split_path(path) == ("uuid:abc-123", "1$4$0")
    assert dlna.split_path("dlna://") == ("", "")


@pytest.fixture
def nas(monkeypatch):
    """One server whose containers are canned Browse answers."""
    server = {"udn": "uuid:abc-123", "name": "NAS", "control": "http://10.0.0.2:8200/ctl/ContentDir",
              "origin": "http://10.0.0.2:8200"}
    tree = {
        "0": ([{"id": "1", "parent": "0", "title": "Music", "count": 2}], []),
        "1": ([{"id": "1$7", "parent": "1", "title": "Album", "count": 1}],
              [{"id": "a", "title": "Song", "url": "http://10.0.0.2:8200/MediaItems/1.mp3", "kind": "audio",
                "size": 10, "artist": "Band", "album": "Record", "mime": "audio/mpeg", "duration": 60.0},
               {"id": "p", "title": "Photo", "url": "http://10.0.0.2:8200/MediaItems/2.jpg", "kind": "image",
                "size": 5, "artist": "", "album": "", "mime": "image/jpeg", "duration": None}]),
        "1$7": ([], [{"id": "b", "title": "Song", "url": "http://10.0.0.2:8200/MediaItems/1.mp3", "kind": "audio",
                      "size": 10, "artist": "Band", "album": "Record", "mime": "audio/mpeg", "duration": 60.0},
                     {"id": "c", "title": "Other", "url": "http://10.0.0.2:8200/MediaItems/3.mkv", "kind": "video",
                      "size": 20, "artist": "", "album": "", "mime": "video/x-matroska", "duration": 90.0}]),
    }
    parents = {"1": "0", "1$7": "1"}
    titles = {"1": "Music", "1$7": "Album"}

    def children(_server, oid):
        containers, items = tree[oid]
        dlna._remember(items)
        return containers, items

    monkeypatch.setattr(dlna, "discover", lambda force=False: [server])
    monkeypatch.setattr(dlna, "_servers", {server["udn"]: server})
    monkeypatch.setattr(dlna, "_titles", {})
    monkeypatch.setattr(dlna, "_containers", {})
    monkeypatch.setattr(dlna, "children", children)
    monkeypatch.setattr(dlna, "_metadata",
                        lambda _s, oid: {"id": oid, "parent": parents.get(oid, "-1"), "title": titles.get(oid, "")})
    return server


def test_the_network_place_lists_the_servers(nas):
    root = dlna.listing("dlna://")
    assert root["parent"] is None and root["error"] is None
    assert root["entries"] == [{"name": "NAS", "path": dlna.make_path("uuid:abc-123", "0"), "kind": "server",
                                "size": None, "is_dir": True}]


def test_a_container_lists_folders_then_media_and_knows_its_parent(nas):
    music = dlna.listing(dlna.make_path("uuid:abc-123", "1"))
    assert music["parent"] == dlna.make_path("uuid:abc-123", "0")
    assert [(e["name"], e["is_dir"], e["kind"]) for e in music["entries"]] == [
        ("Album", True, "folder"), ("Song", False, "audio")]       # the photo only with show_all
    assert len(dlna.listing(dlna.make_path("uuid:abc-123", "1"), show_all=True)["entries"]) == 3
    assert dlna.listing(dlna.make_path("uuid:abc-123", "0"))["parent"] == "dlna://"


def test_the_path_box_reads_as_server_and_folder_names(nas):
    assert dlna.listing("dlna://")["label"] == "Network (DLNA)"
    assert dlna.listing(dlna.make_path("uuid:abc-123", "0"))["label"] == "NAS"
    assert dlna.listing(dlna.make_path("uuid:abc-123", "1$7"))["label"] == "NAS › Music › Album"


def test_labels_use_titles_already_seen_without_asking_the_server(nas, monkeypatch):
    dlna._remember_containers("uuid:abc-123", [{"id": "1", "parent": "0", "title": "Music"},
                                               {"id": "1$7", "parent": "1", "title": "Album"}])
    monkeypatch.setattr(dlna, "_metadata", lambda *_: pytest.fail("asked the server for a title it had"))
    assert dlna.label(nas, "1$7") == "NAS › Music › Album"


def test_expand_walks_containers_once_per_item(nas):
    entries = dlna.expand(dlna.make_path("uuid:abc-123", "1"))
    assert [e["title"] for e in entries] == ["Song", "Other"]       # Song is in both: queued once
    assert dlna.expand(dlna.make_path("uuid:abc-123", "1"), recursive=False) == [entries[0]]
    assert dlna.known(entries[0]["path"]) == {"title": "Song", "subtitle": "Band — Record", "kind": "audio", "art": "",
                                              "size": 10, "duration": 60.0}


def test_only_urls_on_a_known_server_are_accepted(nas):
    dlna.check_url("http://10.0.0.2:8200/MediaItems/1.mp3")
    with pytest.raises(dlna.DlnaError):
        dlna.check_url("http://10.0.0.99:8200/MediaItems/1.mp3")
    with pytest.raises(dlna.DlnaError):
        dlna.check_url("file:///etc/passwd")


def test_a_url_has_an_extension_and_reconnects():
    assert media.extension("http://10.0.0.2:8200/MediaItems/49.mkv?x=1") == ".mkv"
    assert media.extension("/films/a.mp4") == ".mp4"
    assert "-reconnect" in media.input_args("http://10.0.0.2/a.mkv")
    assert media.input_args("/films/a.mkv") == []


ALBUM = """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">
<container id="1$7$1E" parentID="1$7" childCount="2"><dc:title>Songs About Jane</dc:title>
 <upnp:class>object.container.album.musicAlbum</upnp:class>
 <upnp:albumArtURI dlna:profileID="JPEG_TN">http://10.0.0.2:8200/AlbumArt/1-1.jpg</upnp:albumArtURI></container>
</DIDL-Lite>"""

TRACKS = """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">
<item id="t1" parentID="1$7$1E"><dc:title>Harder to Breathe</dc:title><upnp:class>object.item.audioItem.musicTrack</upnp:class>
 <upnp:albumArtURI>http://10.0.0.2:8200/AlbumArt/2-7.jpg</upnp:albumArtURI>
 <res protocolInfo="http-get:*:audio/mpeg:*">http://10.0.0.2:8200/MediaItems/7.mp3</res></item>
<item id="t2" parentID="1$7$1E"><dc:title>This Love</dc:title><upnp:class>object.item.audioItem.musicTrack</upnp:class>
 <res protocolInfo="http-get:*:audio/mpeg:*">http://10.0.0.2:8200/MediaItems/8.mp3</res></item>
</DIDL-Lite>"""


def test_album_art_is_read_from_containers_and_items():
    containers, _ = dlna.parse_didl(ALBUM)
    assert containers[0]["art"] == "http://10.0.0.2:8200/AlbumArt/1-1.jpg"
    _, items = dlna.parse_didl(TRACKS)
    assert [i["art"] for i in items] == ["http://10.0.0.2:8200/AlbumArt/2-7.jpg", ""]
    _, items = dlna.parse_didl(DIDL)
    assert items[0]["art"] == ""


def test_a_track_without_art_shows_its_albums(monkeypatch):
    server = {"udn": "uuid:abc-123", "name": "NAS", "control": "http://10.0.0.2:8200/ctl/ContentDir",
              "origin": "http://10.0.0.2:8200"}
    answers = {"1$7": ALBUM, "1$7$1E": TRACKS}
    monkeypatch.setattr(dlna, "_containers", {})
    monkeypatch.setattr(dlna, "_titles", {})
    monkeypatch.setattr(dlna, "_soap_browse", lambda _s, oid, _flag, _start, _count: (answers[oid].encode(), 1 if oid == "1$7" else 2))
    dlna.children(server, "1$7")                    # the album list: remembers each album's art
    _, items = dlna.children(server, "1$7$1E")
    assert [i["art"] for i in items] == ["http://10.0.0.2:8200/AlbumArt/2-7.jpg",   # its own
                                         "http://10.0.0.2:8200/AlbumArt/1-1.jpg"]   # the album's
    assert dlna.known("http://10.0.0.2:8200/MediaItems/8.mp3")["art"] == "http://10.0.0.2:8200/AlbumArt/1-1.jpg"


class _Upstream:
    def __init__(self, body, kind):
        self.body, self.headers = body, type("H", (), {"get_content_type": lambda _self: kind})()

    def read(self, _n):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.mark.parametrize("kind, ok", [("image/jpeg", True), ("image/svg+xml", False), ("text/html", False)])
def test_album_art_is_served_only_as_a_raster_image(monkeypatch, kind, ok):
    from fastapi import HTTPException

    from services import routes, tokens
    url = f"http://10.0.0.2:8200/AlbumArt/{kind.replace('/', '-')}.jpg"
    monkeypatch.setattr(dlna, "check_url", lambda _url: None)
    monkeypatch.setattr(routes.urllib.request, "urlopen", lambda _url, timeout=0: _Upstream(b"\xff\xd8art", kind))
    token = tokens.token_for(url)
    if ok:
        response = routes.art(token)
        assert (response.body, response.media_type) == (b"\xff\xd8art", "image/jpeg")
    else:
        with pytest.raises(HTTPException):
            routes.art(token)


def test_album_art_from_anywhere_else_is_refused(monkeypatch):
    from fastapi import HTTPException

    from services import routes, tokens
    monkeypatch.setattr(dlna, "_servers", {})
    monkeypatch.setattr(dlna, "discover", lambda force=False: [])
    with pytest.raises(HTTPException) as refused:
        routes.art(tokens.token_for("http://203.0.113.9/AlbumArt/1.jpg"))
    assert refused.value.status_code == 403


# Remembered servers ----------------------------------------------------------------

LOCATION = "http://10.0.0.2:8200/rootDesc.xml"


@pytest.fixture
def network(monkeypatch):
    """A network of device descriptions by address; ``search`` is what SSDP finds."""
    net = {"devices": {}, "search": [], "searches": 0, "hold": None}

    def fetch(url, data=None, headers=None, timeout=None):
        if url not in net["devices"]:
            raise dlna.DlnaError(f"{url} did not answer")
        return net["devices"][url].encode()

    def search(_seconds):
        net["searches"] += 1
        if net["hold"] is not None:
            net["hold"].wait(5)
        return list(net["search"])

    monkeypatch.setattr(dlna, "_fetch", fetch)
    monkeypatch.setattr(dlna, "_search", search)
    monkeypatch.setattr(dlna, "_servers", {})
    monkeypatch.setattr(dlna, "_discovered_at", None)
    monkeypatch.setattr(dlna, "_search_thread", None)
    yield net
    if net["hold"] is not None:
        net["hold"].set()
    if dlna._search_thread is not None:
        dlna._search_thread.join(5)


def _restart(monkeypatch):
    """A new run: nothing in memory, state.json as it was left."""
    from services import store
    if dlna._search_thread is not None:
        dlna._search_thread.join(5)
    store.use_file(None)
    monkeypatch.setattr(dlna, "_servers", {})
    monkeypatch.setattr(dlna, "_discovered_at", None)


def test_a_server_found_once_is_there_on_the_next_run_without_a_search(network, monkeypatch):
    import threading
    network["devices"][LOCATION] = DEVICE
    network["search"] = [LOCATION]
    assert [s["name"] for s in dlna.discover(force=True)] == ["NAS"]

    _restart(monkeypatch)
    network["search"] = []                      # multicast finds nothing this time
    network["hold"] = threading.Event()         # and has not even answered yet
    assert [s["name"] for s in dlna.discover(force=True)] == ["NAS"]
    dlna.check_url("http://10.0.0.2:8200/MediaItems/1.mp3")


def test_a_new_server_joins_when_the_search_behind_finishes(network, monkeypatch):
    network["devices"][LOCATION] = DEVICE
    network["search"] = [LOCATION]
    dlna.discover(force=True)

    _restart(monkeypatch)
    other = "http://10.0.0.3:8200/rootDesc.xml"
    network["devices"][other] = DEVICE.replace("uuid:abc-123", "uuid:other").replace(">NAS<", ">Den<")
    network["search"] = [LOCATION, other]
    dlna.discover(force=True)
    dlna._search_thread.join(5)
    assert [s["name"] for s in dlna.discover(force=True)] == ["Den", "NAS"]


def test_a_remembered_server_that_moved_is_found_by_the_search(network, monkeypatch):
    from services import store
    network["devices"][LOCATION] = DEVICE
    network["search"] = [LOCATION]
    dlna.discover(force=True)

    _restart(monkeypatch)
    moved = "http://10.0.0.7:8200/rootDesc.xml"
    network["devices"] = {moved: DEVICE}
    network["search"] = [moved]
    assert [s["origin"] for s in dlna.discover(force=True)] == ["http://10.0.0.7:8200"]
    assert [s["location"] for s in store.dlna_servers()] == [moved]
    with pytest.raises(dlna.DlnaError):
        dlna.check_url("http://10.0.0.2:8200/MediaItems/1.mp3")     # the old address is nobody now


def test_another_device_at_a_remembered_address_is_not_trusted(network, monkeypatch):
    network["devices"][LOCATION] = DEVICE
    network["search"] = [LOCATION]
    dlna.discover(force=True)

    _restart(monkeypatch)
    network["devices"][LOCATION] = DEVICE.replace("uuid:abc-123", "uuid:someone-else")
    network["search"] = []
    assert dlna.discover(force=True) == []
    with pytest.raises(dlna.DlnaError):
        dlna.check_url("http://10.0.0.2:8200/MediaItems/1.mp3")


def test_a_server_that_is_off_is_not_listed_but_not_forgotten(network, monkeypatch):
    from services import store
    network["devices"][LOCATION] = DEVICE
    network["search"] = [LOCATION]
    dlna.discover(force=True)
    network["devices"], network["search"] = {}, []
    assert dlna.discover(force=True) == []
    assert dlna.listing("dlna://")["error"] == "No media servers answered on your network."
    assert [s["udn"] for s in store.dlna_servers()] == ["uuid:abc-123"]


def test_only_the_most_recent_servers_are_kept():
    from services import store
    store.remember_dlna_servers([{"udn": f"uuid:{n}", "name": str(n), "control": "c", "origin": "o",
                                  "location": f"http://10.0.0.{n}/d.xml"} for n in range(store.DLNA_SERVERS_LIMIT + 5)])
    assert len(store.dlna_servers()) == store.DLNA_SERVERS_LIMIT


def test_open_media_server_goes_back_to_the_folder_last_looked_at(nas, monkeypatch):
    from services import store
    from services.player_service import PlayerService
    service, album = PlayerService(), dlna.make_path("uuid:abc-123", "1$7")
    assert service.browse("dlna://", False, True)["path"] == "dlna://"      # nothing to go back to yet
    assert store.settings()["last_dlna"] == ""                              # the list of servers is not a place
    service.browse(album)
    assert store.settings()["last_dlna"] == album
    assert service.browse("dlna://", False, True)["label"] == "NAS › Music › Album"
    assert service.browse("dlna://")["path"] == "dlna://"                   # the Network place still lists servers

    def off(udn):
        raise dlna.DlnaError("that media server is not on the network now")
    monkeypatch.setattr(dlna, "_server", off)
    back = service.browse("dlna://", False, True)
    assert back["path"] == "dlna://" and [e["name"] for e in back["entries"]] == ["NAS"]
    assert store.settings()["last_dlna"] == album                           # still there when it is back on
