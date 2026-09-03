"""Decode a raw PGS (``.sup``) bitstream into per-cue images (PLAN.md §11, M6).

PGS -- HDMV Presentation Graphic Stream -- is the bitmap subtitle format on
Blu-ray, and therefore the format on most remuxes. ffmpeg can *demux* it
(``-c:s copy`` into the ``sup`` muxer) but there is no way to turn it into text,
so a file whose only subtitles are PGS falls through to a full-file STT pass --
the most expensive thing this pipeline can do. This module is the first half of
avoiding that: bytes in, images out. :mod:`vidcleaner.pipeline.ocr` does the
rest.

Two deliberate properties:

* **Parsing is pure stdlib.** ``parse_sup`` and ``decode_rle`` import nothing
  optional, so the format logic is unit-testable on a checkout with neither
  Pillow nor tesseract installed. Only :func:`render` needs Pillow, and it
  imports it inside the function -- the same boundary
  ``tests/unit/test_no_stt_import.py`` enforces for torch.
* **We parse it ourselves rather than using ``pgsrip``.** See the Decision Log:
  pgsrip 0.1.12 requires ``numpy>=2.2`` and ``setuptools<71`` while this
  project's lock resolves numpy 2.0.2 and setuptools 84 for the ``stt`` extra,
  and the worker needs both extras in one environment. Its segment and RLE
  layout is the reference for what follows (pgsrip, MIT, ratoaq2).

Format, for the reader who has to fix this later. A ``.sup`` is a flat sequence
of segments, each ``PG`` + 32-bit PTS + 32-bit DTS + type + 16-bit length. The
types that matter are PCS (``0x16``, what to show where), WDS (``0x17``, the
regions), PDS (``0x14``, the palette), ODS (``0x15``, the RLE bitmap, possibly
split across several segments) and END (``0x80``). A *display set* is everything
up to an END. A display set carrying composition objects turns subtitles on; the
next one carrying none turns them off, which is where a cue's end time comes
from -- PGS has no duration field.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PGS_TIME_BASE",
    "CompositionObject",
    "DisplaySet",
    "PgsError",
    "PgsFrame",
    "PgsObject",
    "decode_rle",
    "frames",
    "parse_segments",
    "parse_sup",
    "render",
]

#: PGS timestamps are in a 90 kHz clock, like everything else in MPEG.
PGS_TIME_BASE = 90_000.0

_MAGIC = b"PG"
_HEADER = struct.Struct(">2sIIBH")

SEG_PDS = 0x14
SEG_ODS = 0x15
SEG_PCS = 0x16
SEG_WDS = 0x17
SEG_END = 0x80

#: PCS composition_state. An epoch start invalidates every cached palette and
#: object, which is the only reason we track it.
_EPOCH_START = 0x80

#: ODS sequence flags.
_ODS_FIRST = 0x80
_ODS_LAST = 0x40


class PgsError(ValueError):
    """The bitstream is not PGS, or is truncated in a way we cannot skip."""


@dataclass(frozen=True, slots=True)
class PgsObject:
    """One decoded ODS: an RLE bitmap and its dimensions."""

    object_id: int
    width: int
    height: int
    rle: bytes


@dataclass(frozen=True, slots=True)
class CompositionObject:
    """Where a PCS says an object should be drawn."""

    object_id: int
    window_id: int
    x: int
    y: int
    crop: tuple[int, int, int, int] | None = None


@dataclass(slots=True)
class DisplaySet:
    """Everything between two END segments: one screen state."""

    pts_s: float
    width: int = 0
    height: int = 0
    composition: list[CompositionObject] = field(default_factory=list)
    palette: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)
    objects: dict[int, PgsObject] = field(default_factory=dict)

    @property
    def is_clearing(self) -> bool:
        """A display set with nothing to compose is the *end* of the cue before it."""
        return not self.composition


@dataclass(frozen=True, slots=True)
class PgsFrame:
    """A cue: a display set plus the time the next one cleared it."""

    start_s: float
    end_s: float
    display_set: DisplaySet

    @property
    def duration(self) -> float:
        return max(0.0, self.end_s - self.start_s)


# ------------------------------------------------------------------ segments


def parse_segments(data: bytes) -> Iterator[tuple[float, int, bytes]]:
    """Yield ``(pts_seconds, segment_type, payload)`` over a ``.sup``.

    Raises on a bad magic rather than trying to resynchronise: a ``.sup`` we
    wrote ourselves with ``ffmpeg -c:s copy`` is either right or the extraction
    failed, and silently skipping bytes would turn a broken extraction into a
    subtitle track with mysterious holes.
    """
    offset = 0
    end = len(data)
    while offset < end:
        if end - offset < _HEADER.size:
            raise PgsError(f"truncated segment header at byte {offset}")
        magic, pts, _dts, seg_type, size = _HEADER.unpack_from(data, offset)
        if magic != _MAGIC:
            raise PgsError(f"bad magic {magic!r} at byte {offset}; not a PGS stream")
        offset += _HEADER.size
        if end - offset < size:
            raise PgsError(f"truncated {seg_type:#04x} segment at byte {offset}")
        yield pts / PGS_TIME_BASE, seg_type, data[offset : offset + size]
        offset += size


def _parse_pcs(payload: bytes) -> tuple[int, int, int, list[CompositionObject]]:
    if len(payload) < 11:
        raise PgsError("short PCS")
    width, height, _rate, _num, state, _pal_update, _pal_id, count = struct.unpack_from(
        ">HHBHBBBB", payload, 0
    )
    objects: list[CompositionObject] = []
    offset = 11
    for _ in range(count):
        if len(payload) - offset < 8:
            break
        object_id, window_id, cropped, x, y = struct.unpack_from(">HBBHH", payload, offset)
        offset += 8
        crop = None
        if cropped & 0x80:
            if len(payload) - offset < 8:
                break
            crop = struct.unpack_from(">HHHH", payload, offset)
            offset += 8
        objects.append(CompositionObject(object_id, window_id, x, y, crop))
    return width, height, state, objects


def _parse_pds(payload: bytes) -> dict[int, tuple[int, int, int, int]]:
    """Palette entries, converted from YCrCb+alpha to RGBA.

    BT.601 limited range, which is what PGS uses. Entries absent from the
    palette stay fully transparent -- index 0 is the usual background and is
    routinely never defined at all.
    """
    palette: dict[int, tuple[int, int, int, int]] = {}
    for offset in range(2, len(payload) - 4, 5):
        entry, y, cr, cb, alpha = struct.unpack_from(">BBBBB", payload, offset)
        yf = (y - 16) * 1.164383
        crf = cr - 128
        cbf = cb - 128
        red = yf + 1.596027 * crf
        green = yf - 0.391762 * cbf - 0.812968 * crf
        blue = yf + 2.017232 * cbf
        palette[entry] = (
            _clamp8(red),
            _clamp8(green),
            _clamp8(blue),
            alpha,
        )
    return palette


def _clamp8(value: float) -> int:
    return 0 if value < 0 else 255 if value > 255 else int(value + 0.5)


def parse_sup(data: bytes) -> list[DisplaySet]:
    """Every display set in a ``.sup``, in presentation order.

    Palettes and objects persist across display sets within an epoch -- a
    palette-only update repeats neither -- so both are carried forward and reset
    when a PCS declares an epoch start.
    """
    out: list[DisplaySet] = []
    palettes: dict[int, dict[int, tuple[int, int, int, int]]] = {}
    objects: dict[int, PgsObject] = {}
    current: DisplaySet | None = None
    # An ODS split across segments: (object_id, version, width, height, chunks).
    pending: tuple[int, int, int, list[bytes]] | None = None

    for pts_s, seg_type, payload in parse_segments(data):
        if seg_type == SEG_PCS:
            width, height, state, composition = _parse_pcs(payload)
            if state == _EPOCH_START:
                palettes.clear()
                objects.clear()
            current = DisplaySet(pts_s=pts_s, width=width, height=height, composition=composition)
        elif current is None:
            # Segments before the first PCS cannot be placed. Real streams do
            # not do this; a truncated extraction can.
            continue
        elif seg_type == SEG_PDS:
            palette_id = payload[0] if payload else 0
            palettes.setdefault(palette_id, {}).update(_parse_pds(payload))
        elif seg_type == SEG_ODS:
            pending = _accumulate_ods(payload, pending, objects)
        elif seg_type == SEG_END:
            current.palette = dict(palettes.get(0, {}))
            for extra in palettes.values():
                current.palette.update(extra)
            current.objects = dict(objects)
            out.append(current)
            current = None
    return out


def _accumulate_ods(
    payload: bytes,
    pending: tuple[int, int, int, list[bytes]] | None,
    objects: dict[int, PgsObject],
) -> tuple[int, int, int, list[bytes]] | None:
    """Add one ODS segment, completing the object when its last fragment lands.

    An object larger than a segment is split: only the first fragment carries
    the width and height, so the continuation branch has nothing to do but
    append bytes.
    """
    if len(payload) < 4:
        return pending
    object_id, _version, flags = struct.unpack_from(">HBB", payload, 0)
    if flags & _ODS_FIRST:
        if len(payload) < 11:
            return pending
        width, height = struct.unpack_from(">HH", payload, 7)
        pending = (object_id, width, height, [payload[11:]])
    elif pending is not None:
        pending[3].append(payload[4:])
    if flags & _ODS_LAST and pending is not None:
        obj_id, width, height, chunks = pending
        objects[obj_id] = PgsObject(obj_id, width, height, b"".join(chunks))
        return None
    return pending


# ----------------------------------------------------------------------- RLE


def decode_rle(rle: bytes, width: int, height: int) -> bytearray:
    """RLE bitmap -> one palette index per pixel, row-major, ``width*height`` long.

    The encoding, for reference: a non-zero byte is a single pixel of that
    colour. A zero byte introduces a run, whose second byte is ``00`` for
    end-of-line, or two flag bits then a length -- the high bit selects a
    14-bit length over a 6-bit one, and the second bit selects an explicit
    colour byte over colour 0.

    Short rows are padded rather than rejected: a truncated last line is
    common in the wild and costs at most a sliver of one glyph, whereas
    refusing the object loses the whole cue.
    """
    out = bytearray()
    row = bytearray()
    i = 0
    size = len(rle)
    while i < size:
        first = rle[i]
        i += 1
        if first:
            row.append(first)
            continue
        if i >= size:
            break
        second = rle[i]
        i += 1
        if second == 0:
            _flush_row(out, row, width)
            row = bytearray()
            continue
        long_run = second & 0x40
        coloured = second & 0x80
        count = second & 0x3F
        if long_run:
            if i >= size:
                break
            count = (count << 8) | rle[i]
            i += 1
        colour = 0
        if coloured:
            if i >= size:
                break
            colour = rle[i]
            i += 1
        row.extend(bytes([colour]) * count)
    if row:
        _flush_row(out, row, width)
    # Pad or trim to exactly the declared size so callers can trust the length.
    wanted = width * height
    if len(out) < wanted:
        out.extend(b"\x00" * (wanted - len(out)))
    return out[:wanted]


def _flush_row(out: bytearray, row: bytearray, width: int) -> None:
    if len(row) < width:
        row.extend(b"\x00" * (width - len(row)))
    out.extend(row[:width])


# ------------------------------------------------------------------ raster


def render(display_set: DisplaySet, *, background: int = 255) -> Any:
    """Compose a display set into a greyscale ``PIL.Image`` ready for OCR.

    Greyscale with a light background and dark text, because that is what
    tesseract is trained on -- PGS is the other way round (bright text over
    transparency). Alpha is composited against the background rather than
    thresholded, so anti-aliased glyph edges survive; thresholding here
    measurably worsened OCR on thin fonts.

    Returns ``None`` when the display set draws nothing.
    """
    from PIL import Image  # noqa: PLC0415 - optional `ocr` extra, see the module docstring

    if not display_set.composition:
        return None

    canvas = Image.new("L", (display_set.width or 1920, display_set.height or 1080), background)
    drew = False
    for placement in display_set.composition:
        obj = display_set.objects.get(placement.object_id)
        if obj is None or obj.width <= 0 or obj.height <= 0:
            continue
        indices = decode_rle(obj.rle, obj.width, obj.height)
        tile = _tile(Image, indices, obj, display_set.palette, background)
        if tile is None:
            continue
        canvas.paste(tile, (placement.x, placement.y))
        drew = True
    return canvas if drew else None


def _tile(image_module: Any, indices: bytearray, obj: PgsObject, palette, background: int):
    """One object as a greyscale tile: bright-over-transparent, inverted.

    The composite is onto black -- what a viewer sees, since we have no video --
    and then inverted, so the bright glyph body becomes dark on light. A plain
    composite onto a light background would render white text invisible, which
    is exactly the bug this replaced.

    The usual black outline inverts to the background value and vanishes. That
    is fine, and better than the alternative: tesseract wants glyph bodies, and
    the outline only ever existed to separate them from the video.
    """
    grey = bytearray(len(indices))
    # Cache per index: a subtitle uses a handful of the 256 palette entries.
    resolved: dict[int, int] = {}
    for position, index in enumerate(indices):
        value = resolved.get(index)
        if value is None:
            red, green, blue, alpha = palette.get(index, (0, 0, 0, 0))
            luma = (red * 299 + green * 587 + blue * 114) // 1000
            value = background - (luma * alpha) // 255
            resolved[index] = max(0, min(255, value))
        grey[position] = value
    if all(value >= background for value in resolved.values()):
        return None
    return image_module.frombytes("L", (obj.width, obj.height), bytes(grey))


# ----------------------------------------------------------------------- cues


def frames(display_sets: list[DisplaySet]) -> list[PgsFrame]:
    """Pair each drawing display set with the one that clears it.

    PGS carries no duration: a cue lasts until the next display set says
    otherwise. A drawing set immediately followed by another drawing set (a
    caption that changes without a gap) ends where the next one starts. A final
    set that is never cleared -- a truncated extraction -- is given a nominal
    two seconds rather than being dropped, since its text is still a perfectly
    good STT window.
    """
    out: list[PgsFrame] = []
    for position, current in enumerate(display_sets):
        if current.is_clearing:
            continue
        end = None
        for later in display_sets[position + 1 :]:
            if later.pts_s > current.pts_s:
                end = later.pts_s
                break
        if end is None:
            end = current.pts_s + 2.0
        out.append(PgsFrame(start_s=current.pts_s, end_s=end, display_set=current))
    return out
