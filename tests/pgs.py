"""
A Blu-ray PGS subtitle stream (``.sup``) written by hand, for the tests: ffmpeg
decodes image subtitles but cannot encode PGS, so there is no other way to
make one. Each subtitle is one opaque rectangle of one colour.

Muxed into a film as its own input, ffmpeg starts it at its first subtitle:
give the first box the start time 0 and the times are the film's.
"""
import struct


def _segment(kind: int, pts: float, data: bytes) -> bytes:
    return b"PG" + struct.pack(">IIBH", int(pts * 90000), 0, kind, len(data)) + data


def _rle(width: int, height: int, colour: int) -> bytes:
    # 00 11LLLLLL LLLLLLLL CC: a run of L pixels of colour CC; 00 00 ends the line.
    line = bytes([0x00, 0xC0 | (width >> 8), width & 0xFF, colour, 0x00, 0x00])
    return line * height


def sup(boxes: list[tuple[float, float, int, int, int, int]], canvas: tuple[int, int] = (1920, 1080),
        ycrcb: tuple[int, int, int] = (105, 212, 212)) -> bytes:
    """``boxes``: (start, end, x, y, width, height) in seconds and canvas pixels. Magenta unless told otherwise."""
    out = b""
    for number, (start, end, x, y, w, h) in enumerate(boxes):
        size = struct.pack(">HH", *canvas)
        window = struct.pack(">BBHHHH", 1, 0, x, y, w, h)
        rle = _rle(w, h, 1)
        # Shown: composition (epoch start, one object), window, palette, object, end.
        out += _segment(0x16, start, size + struct.pack(">BHBBBB", 0x10, number * 2, 0x80, 0, 0, 1)
                        + struct.pack(">HBBHH", 0, 0, 0, x, y))
        out += _segment(0x17, start, window)
        out += _segment(0x14, start, bytes([0, 0, 1, *ycrcb, 255]))
        out += _segment(0x15, start, struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big")
                        + struct.pack(">HH", w, h) + rle)
        out += _segment(0x80, start, b"")
        # Cleared: a composition with no objects.
        out += _segment(0x16, end, size + struct.pack(">BHBBBB", 0x10, number * 2 + 1, 0x00, 0, 0, 0))
        out += _segment(0x17, end, window)
        out += _segment(0x80, end, b"")
    return out
