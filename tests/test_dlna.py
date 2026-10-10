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
                      "origin": "http://10.0.0.2:8200"}


def test_xml_with_a_dtd_is_refused():
    with pytest.raises(dlna.DlnaError):
        dlna.parse_didl('<!DOCTYPE x [<!ENTITY a "aaaa">]><DIDL-Lite>&a;</DIDL-Lite>')


def test_didl_gives_containers_and_playable_items_in_order():
    containers, items = dlna.parse_didl(DIDL)
    assert containers == [{"id": "1$7", "parent": "1", "title": "Album", "count": 59}]
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

    def children(_server, oid):
        containers, items = tree[oid]
        dlna._remember(items)
        return containers, items

    monkeypatch.setattr(dlna, "discover", lambda force=False: [server])
    monkeypatch.setattr(dlna, "_servers", {server["udn"]: server})
    monkeypatch.setattr(dlna, "_titles", {})
    monkeypatch.setattr(dlna, "children", children)
    monkeypatch.setattr(dlna, "_metadata", lambda _s, oid: {"id": oid, "parent": parents.get(oid, "-1"), "title": ""})
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


def test_expand_walks_containers_once_per_item(nas):
    entries = dlna.expand(dlna.make_path("uuid:abc-123", "1"))
    assert [e["title"] for e in entries] == ["Song", "Other"]       # Song is in both: queued once
    assert dlna.expand(dlna.make_path("uuid:abc-123", "1"), recursive=False) == [entries[0]]
    assert dlna.known(entries[0]["path"]) == {"title": "Song", "subtitle": "Band — Record", "kind": "audio"}


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
