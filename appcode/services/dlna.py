"""
DLNA / UPnP media servers on the local network (a NAS running MiniDLNA, say).

Discovery is SSDP: one multicast M-SEARCH for MediaServer devices, then each
device's description for its name and ContentDirectory control URL. Browsing
is the ContentDirectory ``Browse`` SOAP action; its answer is DIDL-Lite XML.

The Open dialog sees servers and their containers as folders with
``dlna://<udn>/<object id>`` paths. A server's media items are plain http URLs,
and those are what goes in the queue: ``prepare`` probes and transcodes them
like local files (ffmpeg reads http and seeks with Range requests).

Only URLs on a discovered server are accepted for playback, so the page cannot
make the server fetch arbitrary addresses.

Plain module (not a BFF file): the server table and titles live for the life
of the process.
"""
from __future__ import annotations

import html
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import quote, unquote, urljoin, urlsplit

SCHEME = "dlna://"
ROOT_LABEL = "Network (DLNA)"
SSDP_ADDR = ("239.255.255.250", 1900)
MEDIA_SERVER = "urn:schemas-upnp-org:device:MediaServer:1"
CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
DISCOVER_SECONDS = 2.0
REDISCOVER_AFTER = 60.0       # seconds a discovery is trusted for
HTTP_TIMEOUT = 10
PAGE = 500                    # Browse results per request
MAX_CHILDREN = 5000           # one container, as browse.MAX_EXPAND for folders
MAX_EXPAND = 5000
MAX_DEPTH = 32                # containers walked up for a readable label

_servers: dict[str, dict] = {}        # udn -> {udn, name, control, origin}
_discovered_at = 0.0
_titles: dict[str, dict] = {}         # media url -> {title, subtitle, kind}
_containers: dict[tuple[str, str], dict] = {}   # (udn, container id) -> {title, parent}
_lock = threading.Lock()


class DlnaError(Exception):
    pass


# Paths and URLs ----------------------------------------------------------------

def is_dlna_path(path: str) -> bool:
    return path.startswith(SCHEME)


def is_url(path: str) -> bool:
    return path.startswith(("http://", "https://"))


def make_path(udn: str, object_id: str) -> str:
    return f"{SCHEME}{quote(udn, safe='')}/{quote(object_id, safe='')}"


def split_path(path: str) -> tuple[str, str]:
    """``dlna://<udn>/<id>`` -> (udn, id); the root ``dlna://`` -> ("", "")."""
    rest = path[len(SCHEME):].strip("/")
    if not rest:
        return "", ""
    udn, _, object_id = rest.partition("/")
    return unquote(udn), unquote(object_id) or "0"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


# XML ---------------------------------------------------------------------------

def _xml(text: bytes | str) -> ET.Element:
    """Parse XML from the network, refusing DTDs (entity expansion)."""
    raw = text.encode() if isinstance(text, str) else text
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise DlnaError("the server sent XML with a DTD")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as error:
        raise DlnaError(f"the server sent bad XML ({error})") from error


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> Optional[ET.Element]:
    return next((c for c in node if _local(c.tag) == name), None)


def _text(node: ET.Element, name: str) -> str:
    found = _child(node, name)
    return (found.text or "").strip() if found is not None else ""


# Parsing (pure) ----------------------------------------------------------------

def parse_ssdp(data: bytes) -> Optional[dict]:
    """One SSDP answer -> {location, st, usn, server}, or None if it is not one."""
    lines = data.decode("utf-8", errors="replace").split("\r\n")
    if not lines or not lines[0].upper().startswith(("HTTP/1.1 200", "NOTIFY")):
        return None
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if not headers.get("location", "").startswith(("http://", "https://")):
        return None
    return {"location": headers["location"], "st": headers.get("st") or headers.get("nt", ""),
            "usn": headers.get("usn", ""), "server": headers.get("server", "")}


def parse_device(xml: bytes | str, location: str) -> Optional[dict]:
    """A device description -> {udn, name, control, origin} for a MediaServer, else None."""
    root = _xml(xml)
    base = _text(root, "URLBase") or location
    for device in root.iter():
        if _local(device.tag) != "device" or "MediaServer" not in _text(device, "deviceType"):
            continue
        service_list = _child(device, "serviceList")
        for service in (service_list if service_list is not None else []):
            if _text(service, "serviceType").startswith("urn:schemas-upnp-org:service:ContentDirectory:"):
                control = urljoin(base, _text(service, "controlURL"))
                return {"udn": _text(device, "UDN"), "name": _text(device, "friendlyName") or urlsplit(location).hostname,
                        "control": control, "origin": _origin(location)}
    return None


def _duration(value: str) -> Optional[float]:
    """DIDL ``H:MM:SS.fff`` -> seconds."""
    try:
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def _kind(upnp_class: str, mime: str) -> str:
    if upnp_class.startswith("object.item.videoItem") or mime.startswith("video/"):
        return "video"
    if upnp_class.startswith("object.item.audioItem") or mime.startswith("audio/"):
        return "audio"
    if upnp_class.startswith("object.item.imageItem") or mime.startswith("image/"):
        return "image"
    return "file"


def _art(node: ET.Element) -> str:
    """The first http ``upnp:albumArtURI`` of a container or item, else ""."""
    return next(((c.text or "").strip() for c in node
                 if _local(c.tag) == "albumArtURI" and is_url((c.text or "").strip())), "")


def parse_didl(xml: bytes | str) -> tuple[list[dict], list[dict]]:
    """A Browse result -> (containers, items), in the server's order."""
    root = _xml(xml)
    containers, items = [], []
    for node in root:
        name = _local(node.tag)
        title = _text(node, "title") or node.get("id", "")
        if name == "container":
            count = node.get("childCount")
            containers.append({"id": node.get("id", ""), "parent": node.get("parentID", ""), "title": title,
                               "count": int(count) if count and count.isdigit() else None, "art": _art(node)})
        elif name == "item":
            res = next((r for r in node if _local(r.tag) == "res"
                        and (r.get("protocolInfo") or "").startswith("http-get:") and is_url((r.text or "").strip())), None)
            if res is None:
                continue
            mime = (res.get("protocolInfo") or "").split(":")[2] if res.get("protocolInfo", "").count(":") >= 2 else ""
            size = res.get("size")
            items.append({
                "id": node.get("id", ""), "title": title, "url": (res.text or "").strip(), "mime": mime,
                "kind": _kind(_text(node, "class"), mime), "duration": _duration(res.get("duration", "")),
                "size": int(size) if size and size.isdigit() else None,
                "artist": _text(node, "artist") or _text(node, "creator"), "album": _text(node, "album"),
                "art": _art(node),
            })
    return containers, items


# Network -----------------------------------------------------------------------

def _search(seconds: float) -> list[str]:
    """Locations of the MediaServers that answer an M-SEARCH within ``seconds``."""
    message = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: {max(1, int(seconds))}\r\nST: {MEDIA_SERVER}\r\n\r\n").encode()
    found: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.3)
        sock.bind(("", 0))
        for _ in range(2):  # UDP: ask twice
            sock.sendto(message, SSDP_ADDR)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            answer = parse_ssdp(data)
            if answer and answer["location"] not in found:
                found.append(answer["location"])
    except OSError as error:
        raise DlnaError(f"cannot search the network ({error})") from error
    finally:
        sock.close()
    return found


def _fetch(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.read(16 * 1024 * 1024)
    except (urllib.error.URLError, OSError) as error:
        raise DlnaError(f"{urlsplit(url).netloc} did not answer ({getattr(error, 'reason', error)})") from error


def discover(force: bool = False) -> list[dict]:
    """The media servers on the network, discovered at most once a minute."""
    global _discovered_at
    with _lock:
        if not force and _servers and time.monotonic() - _discovered_at < REDISCOVER_AFTER:
            return sorted(_servers.values(), key=lambda s: s["name"].lower())
    found = {}
    for location in _search(DISCOVER_SECONDS):
        try:
            server = parse_device(_fetch(location), location)
        except DlnaError:
            continue
        if server and server["udn"]:
            found[server["udn"]] = server
    with _lock:
        _servers.clear()
        _servers.update(found)
        _discovered_at = time.monotonic()
        return sorted(_servers.values(), key=lambda s: s["name"].lower())


def _server(udn: str) -> dict:
    with _lock:
        server = _servers.get(udn)
    if server is None:
        server = next((s for s in discover(force=True) if s["udn"] == udn), None)
    if server is None:
        raise DlnaError("that media server is not on the network now")
    return server


def _soap_browse(server: dict, object_id: str, flag: str, start: int, count: int) -> tuple[bytes, int]:
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:Browse xmlns:u="{CONTENT_DIRECTORY}">'
        f"<ObjectID>{html.escape(object_id)}</ObjectID><BrowseFlag>{flag}</BrowseFlag>"
        f"<Filter>*</Filter><StartingIndex>{start}</StartingIndex><RequestedCount>{count}</RequestedCount>"
        "<SortCriteria></SortCriteria></u:Browse></s:Body></s:Envelope>"
    ).encode()
    answer = _xml(_fetch(server["control"], body, {
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{CONTENT_DIRECTORY}#Browse"'}))
    result = next((n for n in answer.iter() if _local(n.tag) == "Result"), None)
    if result is None:
        fault = next((n.text for n in answer.iter() if _local(n.tag) in ("errorDescription", "faultstring")), None)
        raise DlnaError(fault or "the server did not answer the Browse request")
    total = next((n.text for n in answer.iter() if _local(n.tag) == "TotalMatches"), "0") or "0"
    return (result.text or "").encode(), int(total) if total.isdigit() else 0


def children(server: dict, object_id: str) -> tuple[list[dict], list[dict]]:
    """Every container and item directly in ``object_id`` (up to MAX_CHILDREN)."""
    containers, items, start = [], [], 0
    while start < MAX_CHILDREN:
        result, total = _soap_browse(server, object_id, "BrowseDirectChildren", start, PAGE)
        page_containers, page_items = parse_didl(result) if result.strip() else ([], [])
        containers += page_containers
        items += page_items
        got = len(page_containers) + len(page_items)
        start += PAGE
        if not got or start >= total:
            break
    # A track without art of its own shows its album's (the container's).
    with _lock:
        album_art = (_containers.get((server["udn"], object_id)) or {}).get("art", "")
    if album_art:
        items = [item if item.get("art") else {**item, "art": album_art} for item in items]
    _remember(items)
    _remember_containers(server["udn"], containers)
    return containers, items


def _metadata(server: dict, object_id: str) -> dict:
    """The container itself: its title and parent."""
    result, _total = _soap_browse(server, object_id, "BrowseMetadata", 0, 1)
    containers, _items = parse_didl(result) if result.strip() else ([], [])
    _remember_containers(server["udn"], containers)
    return containers[0] if containers else {"id": object_id, "parent": "-1", "title": ""}


def _remember_containers(udn: str, containers: list[dict]) -> None:
    with _lock:
        if len(_containers) > 20000:
            _containers.clear()
        for c in containers:
            _containers[(udn, c["id"])] = {"title": c["title"], "parent": c["parent"], "art": c.get("art", "")}


def label(server: dict, object_id: str) -> str:
    """``Server › Folder › Folder`` for a container, from titles seen while browsing."""
    names: list[str] = []
    oid = object_id
    for _ in range(MAX_DEPTH):
        if oid in ("", "0", "-1"):
            break
        with _lock:
            here = _containers.get((server["udn"], oid))
        if here is None:
            here = _metadata(server, oid)
        names.append(here.get("title") or oid)
        oid = here.get("parent") or ""
    return " › ".join([server["name"], *reversed(names)])


def _remember(items: list[dict]) -> None:
    with _lock:
        if len(_titles) > 20000:
            _titles.clear()
        for item in items:
            subtitle = " — ".join(x for x in (item.get("artist"), item.get("album")) if x)
            _titles[item["url"]] = {"title": item["title"], "subtitle": subtitle, "kind": item["kind"],
                                    "art": item.get("art", "")}


# What the BFF uses ---------------------------------------------------------------

def known(url: str) -> Optional[dict]:
    """The title, subtitle and kind a server gave this URL, if it was browsed."""
    with _lock:
        found = _titles.get(url)
        return dict(found) if found else None


def check_url(url: str) -> None:
    """Refuse a URL that is not on a discovered media server."""
    if not is_url(url):
        raise DlnaError("not an http address")
    origin = _origin(url)
    with _lock:
        if any(s["origin"] == origin for s in _servers.values()):
            return
    if not any(s["origin"] == origin for s in discover(force=True)):
        raise DlnaError("it is not on a media server on your network (is the server on?)")


def listing(path: str, show_all: bool = False) -> dict:
    """The Open dialog's view of ``dlna://…``, in the shape of ``browse.listing``."""
    udn, object_id = split_path(path)
    if not udn:
        try:
            servers = discover(force=True)
        except DlnaError as error:
            return {"path": SCHEME, "label": ROOT_LABEL, "parent": None, "entries": [], "error": str(error)}
        return {"path": SCHEME, "label": ROOT_LABEL, "parent": None,
                "error": None if servers else "No media servers answered on your network.",
                "entries": [{"name": s["name"], "path": make_path(s["udn"], "0"), "kind": "server", "size": None,
                             "is_dir": True} for s in servers]}
    try:
        server = _server(udn)
        here = _metadata(server, object_id) if object_id != "0" else {"parent": "-1"}
        containers, items = children(server, object_id)
        readable = label(server, object_id)
    except DlnaError as error:
        return {"path": path, "parent": SCHEME, "entries": [], "error": str(error)}
    parent = here.get("parent")
    entries = [{"name": c["title"], "path": make_path(udn, c["id"]), "kind": "folder", "size": None, "is_dir": True}
               for c in containers]
    entries += [{"name": i["title"], "path": i["url"], "kind": i["kind"], "size": i["size"], "is_dir": False}
                for i in items if show_all or i["kind"] in ("video", "audio")]
    return {"path": make_path(udn, object_id), "label": readable, "entries": entries, "error": None,
            "parent": make_path(udn, parent) if parent and parent != "-1" else SCHEME}


def expand(path: str, recursive: bool = True) -> list[dict]:
    """A container (and, with ``recursive``, those below it) as queue entries."""
    udn, object_id = split_path(path)
    if not udn:
        return []
    server = _server(udn)
    out: list[dict] = []
    seen: set[str] = set()
    visited: set[str] = set()

    def walk(oid: str) -> None:
        if oid in visited or len(out) >= MAX_EXPAND:
            return
        visited.add(oid)
        containers, items = children(server, oid)
        for item in items:
            if item["kind"] in ("video", "audio") and item["url"] not in seen:
                seen.add(item["url"])
                out.append({"path": item["url"], "title": item["title"], "kind": item["kind"]})
                if len(out) >= MAX_EXPAND:
                    return
        if recursive:
            for container in containers:
                walk(container["id"])

    walk(object_id)
    return out


def entry(url: str) -> dict:
    """One media URL as a queue entry, with the title the server gave it."""
    meta = known(url) or {}
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).rsplit(".", 1)[0] or url
    return {"path": url, "title": meta.get("title") or name, "kind": meta.get("kind") or "video"}
