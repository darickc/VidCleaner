"""Write a minimal but valid PGS (``.sup``) bitstream, for test fixtures only.

This exists because of a gap recorded in PLAN.md's Decision Log on 2026-09-01:
ffmpeg has no PGS *encoder* (it decodes ``pgssub`` and muxes ``sup``, but
"subtitle encoding [is] only possible from text to text or bitmap to bitmap"),
so a bitmap-subtitle fixture could not be generated and the rule that bitmap
subs are copied untouched was only ever unit-tested over recorded ffprobe JSON.

Rasterising the cues with Pillow's *bundled* font and emitting the segments by
hand closes that gap without committing binary media or hitting the network,
which keeps ``make_fixtures``' contract intact. It is also the strongest
available test of :mod:`vidcleaner.pipeline.pgs`: writer and parser are
independent implementations of the same table, so a round-trip that recovers the
original pixels exercises both, and ffmpeg muxing the result proves the bytes
are really PGS rather than merely self-consistent.

Deliberately minimal: one window, one object per cue, one palette, no cropping
and no fragmented objects. The parser handles all of those; they just cannot be
produced from here, and the unit tests build those cases byte-wise instead.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = ["PgsCue", "encode_rle", "render_indices", "write_sup"]

_MAGIC = b"PG"
_HEADER = struct.Struct(">2sIIBH")
_TIME_BASE = 90_000

SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_PCS = 0x16
SEG_WDS = 0x17
SEG_END = 0x80

#: Palette index 1 is opaque white text; index 0 is left transparent, exactly as
#: a real stream leaves its background.
_TEXT_INDEX = 1

#: Keep glyphs off the band edge, where a crop can clip them.
_MARGIN_PX = 24


@dataclass(frozen=True, slots=True)
class PgsCue:
    start_s: float
    end_s: float
    text: str


def _segment(seg_type: int, pts_s: float, payload: bytes) -> bytes:
    pts = int(round(pts_s * _TIME_BASE))
    return _HEADER.pack(_MAGIC, pts, 0, seg_type, len(payload)) + payload


def encode_rle(indices: bytes, width: int, height: int) -> bytes:
    """Palette indices -> PGS run-length encoding, one terminated run per row.

    The four run forms are the ones :func:`vidcleaner.pipeline.pgs.decode_rle`
    documents: a bare non-zero byte is a single pixel, and a leading zero
    introduces a run whose flags choose a 6- or 14-bit length and an implicit
    colour 0 or an explicit one.
    """
    out = bytearray()
    for row_start in range(0, width * height, width):
        row = indices[row_start : row_start + width]
        position = 0
        while position < len(row):
            colour = row[position]
            run = 1
            while position + run < len(row) and row[position + run] == colour and run < 16383:
                run += 1
            out.extend(_run(colour, run))
            position += run
        out.extend(b"\x00\x00")  # end of line
    return bytes(out)


def _run(colour: int, count: int) -> bytes:
    if colour == 0:
        if count <= 63:
            return bytes([0x00, count])
        return bytes([0x00, 0x40 | (count >> 8), count & 0xFF])
    if count <= 2:
        # A single byte per pixel is shorter than any run form.
        return bytes([colour]) * count
    if count <= 63:
        return bytes([0x00, 0x80 | count, colour])
    return bytes([0x00, 0xC0 | (count >> 8), count & 0xFF, colour])


def render_indices(text: str, width: int, height: int, *, font_size: int = 48) -> bytes:
    """Rasterise ``text`` centred on a transparent band, as palette indices.

    Pillow's ``load_default(size=...)`` returns a real scalable TrueType face
    (Aileron, bundled base64 in the wheel), so this needs no font file on disk
    and behaves identically on every machine and both architectures.
    """
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415 - fixture tooling only

    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=font_size)
    # Wrap like a real subtitle rather than letting a long line run off the
    # band: an overflowing cue silently loses its tail, which reads as an OCR
    # recall problem when it is really a rasteriser one.
    wrapped = _wrap(draw, text, font, width - 2 * _MARGIN_PX)
    left, top, right, bottom = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center")
    draw.multiline_text(
        ((width - (right - left)) // 2 - left, (height - (bottom - top)) // 2 - top),
        wrapped,
        font=font,
        fill=255,
        align="center",
    )
    # Anything the rasteriser touched becomes text; PGS is palettised, and a
    # two-entry palette is all a legibility fixture needs.
    return bytes(_TEXT_INDEX if pixel else 0 for pixel in image.tobytes())


def _wrap(draw, text: str, font, max_width: int) -> str:
    """Greedy word wrap to ``max_width`` pixels; at most two lines, like a cue."""
    words = text.split()
    if not words:
        return text
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return "\n".join(lines)


def _pds() -> bytes:
    """Palette 0: index 1 opaque white, index 0 transparent black."""
    entries = bytes([_TEXT_INDEX, 235, 128, 128, 255]) + bytes([0, 16, 128, 128, 0])
    return bytes([0, 0]) + entries


def _pcs(width: int, height: int, number: int, *, objects: bool, x: int, y: int) -> bytes:
    head = struct.pack(
        ">HHBHBBBB",
        width,
        height,
        0x10,  # frame rate; ignored by every decoder including ours
        number,
        0x80 if objects else 0x00,  # epoch start for a drawing set
        0,
        0,
        1 if objects else 0,
    )
    if not objects:
        return head
    return head + struct.pack(">HBBHH", 0, 0, 0, x, y)


def _wds(x: int, y: int, width: int, height: int) -> bytes:
    return bytes([1]) + struct.pack(">BHHHH", 0, x, y, width, height)


def _ods(indices: bytes, width: int, height: int) -> bytes:
    rle = encode_rle(indices, width, height)
    # The declared length covers the width and height fields as well as the RLE.
    length = len(rle) + 4
    return (
        struct.pack(">HBB", 0, 0, 0xC0)  # object 0, version 0, first AND last
        + bytes([(length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF])
        + struct.pack(">HH", width, height)
        + rle
    )


def write_sup(
    cues: list[PgsCue],
    *,
    video_width: int = 1920,
    video_height: int = 1080,
    band_height: int = 120,
    font_size: int = 48,
) -> bytes:
    """A complete ``.sup`` for ``cues``, ready for ``ffmpeg -c:s copy``.

    Each cue becomes a drawing display set at its start and a clearing one at
    its end -- which is how PGS expresses duration, since a cue carries none.
    """
    band_width = video_width
    x = 0
    y = max(0, video_height - band_height - 40)
    out = bytearray()
    for number, cue in enumerate(cues):
        indices = render_indices(cue.text, band_width, band_height, font_size=font_size)
        out += _segment(
            SEG_PCS,
            cue.start_s,
            _pcs(video_width, video_height, number * 2, objects=True, x=x, y=y),
        )
        out += _segment(SEG_WDS, cue.start_s, _wds(x, y, band_width, band_height))
        out += _segment(SEG_PDS, cue.start_s, _pds())
        out += _segment(SEG_ODS, cue.start_s, _ods(indices, band_width, band_height))
        out += _segment(SEG_END, cue.start_s, b"")

        out += _segment(
            SEG_PCS,
            cue.end_s,
            _pcs(video_width, video_height, number * 2 + 1, objects=False, x=x, y=y),
        )
        out += _segment(SEG_WDS, cue.end_s, _wds(x, y, band_width, band_height))
        out += _segment(SEG_END, cue.end_s, b"")
    return bytes(out)
