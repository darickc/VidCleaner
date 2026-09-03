"""The PGS bitstream parser (M6).

Two kinds of test here. The round-trips go through ``scripts.pgs_writer``, which
is an independent implementation of the same tables, so agreement means both are
right rather than consistently wrong. The byte-level tests build segments by
hand, because the writer deliberately cannot produce the awkward cases the
parser has to survive -- fragmented objects, palette reuse, truncation.
"""

from __future__ import annotations

import struct

import pytest

from scripts.pgs_writer import PgsCue, encode_rle, write_sup
from vidcleaner.pipeline import pgs

HEADER = struct.Struct(">2sIIBH")


def seg(seg_type: int, pts_s: float, payload: bytes) -> bytes:
    return HEADER.pack(b"PG", int(pts_s * 90_000), 0, seg_type, len(payload)) + payload


# ------------------------------------------------------------------- segments


def test_segments_carry_type_and_ninety_kilohertz_time():
    data = seg(pgs.SEG_PCS, 1.5, b"\x00" * 11) + seg(pgs.SEG_END, 1.5, b"")
    parsed = list(pgs.parse_segments(data))
    assert [(round(t, 3), kind) for t, kind, _ in parsed] == [
        (1.5, pgs.SEG_PCS),
        (1.5, pgs.SEG_END),
    ]


def test_a_stream_that_is_not_pgs_is_rejected_rather_than_resynchronised():
    # Silently skipping bytes would turn a broken extraction into a subtitle
    # track with mysterious holes, which is far worse to debug.
    with pytest.raises(pgs.PgsError, match="not a PGS stream"):
        list(pgs.parse_segments(b"NOPE" + b"\x00" * 20))


def test_a_truncated_segment_is_reported_not_silently_dropped():
    data = seg(pgs.SEG_PCS, 0.0, b"\x00" * 11)[:-4]
    with pytest.raises(pgs.PgsError, match="truncated"):
        list(pgs.parse_segments(data))


# ------------------------------------------------------------------------ RLE


@pytest.mark.parametrize(
    ("indices", "width", "height"),
    [
        (bytes([0] * 16), 16, 1),
        (bytes([1] * 16), 16, 1),
        (bytes([0, 1] * 8), 16, 1),
        (bytes([1] * 200), 200, 1),  # forces the 14-bit run form
        (bytes([0] * 100 + [1] * 100), 100, 2),
    ],
)
def test_rle_round_trips(indices, width, height):
    assert bytes(pgs.decode_rle(encode_rle(indices, width, height), width, height)) == indices


def test_each_run_form_decodes():
    # single pixel | short colour run | short zero run | long zero run | EOL
    rle = bytes([0x05]) + bytes([0x00, 0x83, 0x07]) + bytes([0x00, 0x04]) + bytes([0x00, 0x00])
    row = pgs.decode_rle(rle, 8, 1)
    assert list(row) == [5, 7, 7, 7, 0, 0, 0, 0]


def test_a_short_row_is_padded_rather_than_losing_the_object():
    # A truncated final line costs a sliver of one glyph; refusing the object
    # would lose the whole cue.
    row = pgs.decode_rle(bytes([0x09, 0x00, 0x00]), 6, 1)
    assert list(row) == [9, 0, 0, 0, 0, 0]


def test_the_decoded_length_always_matches_the_declared_size():
    assert len(pgs.decode_rle(b"", 32, 4)) == 128


# --------------------------------------------------------------- display sets


def test_a_written_sup_parses_back_to_the_cues_it_was_built_from():
    cues = [PgsCue(1.0, 3.0, "Oh shit"), PgsCue(4.0, 6.5, "You idiot")]
    sets = pgs.parse_sup(write_sup(cues, video_width=640, video_height=360, band_height=64))
    # One drawing set and one clearing set per cue.
    assert len(sets) == 4
    assert [s.is_clearing for s in sets] == [False, True, False, True]

    frames = pgs.frames(sets)
    assert [(round(f.start_s, 2), round(f.end_s, 2)) for f in frames] == [(1.0, 3.0), (4.0, 6.5)]


def test_an_object_split_across_segments_is_reassembled():
    # The writer never fragments, so this is built by hand -- and it is the case
    # most likely to appear in a real Blu-ray rip, where objects exceed 64 KiB.
    width, height = 8, 1
    rle = encode_rle(bytes([3] * 8), width, height)
    first = (
        struct.pack(">HBB", 7, 0, 0x80)
        + bytes([0, 0, len(rle) + 4])
        + struct.pack(">HH", width, height)
    )
    split = len(rle) // 2
    data = (
        seg(
            pgs.SEG_PCS,
            0.0,
            struct.pack(">HHBHBBBB", 640, 360, 0x10, 0, 0x80, 0, 0, 1)
            + struct.pack(">HBBHH", 7, 0, 0, 0, 0),
        )
        + seg(pgs.SEG_ODS, 0.0, first + rle[:split])
        + seg(pgs.SEG_ODS, 0.0, struct.pack(">HBB", 7, 0, 0x40) + rle[split:])
        + seg(pgs.SEG_END, 0.0, b"")
    )
    sets = pgs.parse_sup(data)
    assert list(sets[0].objects) == [7]
    assert sets[0].objects[7].rle == rle


def test_palettes_persist_across_display_sets_until_an_epoch_starts():
    # A palette-only update does not repeat the PDS, so a parser that forgot
    # the palette between display sets would render the second cue blank.
    pcs_draw = struct.pack(">HHBHBBBB", 640, 360, 0x10, 0, 0x80, 0, 0, 1) + struct.pack(
        ">HBBHH", 0, 0, 0, 0, 0
    )
    pcs_again = struct.pack(">HHBHBBBB", 640, 360, 0x10, 1, 0x00, 0, 0, 1) + struct.pack(
        ">HBBHH", 0, 0, 0, 0, 0
    )
    pds = bytes([0, 0]) + bytes([1, 235, 128, 128, 255])
    data = (
        seg(pgs.SEG_PCS, 0.0, pcs_draw)
        + seg(pgs.SEG_PDS, 0.0, pds)
        + seg(pgs.SEG_END, 0.0, b"")
        + seg(pgs.SEG_PCS, 5.0, pcs_again)
        + seg(pgs.SEG_END, 5.0, b"")
    )
    sets = pgs.parse_sup(data)
    assert sets[1].palette.get(1) is not None, "palette must survive into the next display set"


def test_an_epoch_start_clears_cached_objects():
    sets = pgs.parse_sup(
        write_sup(
            [PgsCue(0.0, 1.0, "a"), PgsCue(2.0, 3.0, "b")],
            video_width=320,
            video_height=240,
            band_height=48,
            font_size=20,
        )
    )
    # Each drawing set declares an epoch start, so it carries exactly its own object.
    assert all(len(s.objects) == 1 for s in sets if not s.is_clearing)


# ---------------------------------------------------------------------- cues


def test_a_cue_that_is_never_cleared_still_gets_a_window():
    # A truncated extraction loses the trailing clear; the text is still a
    # perfectly good STT window, so it is kept with a nominal duration.
    pcs = struct.pack(">HHBHBBBB", 640, 360, 0x10, 0, 0x80, 0, 0, 1) + struct.pack(
        ">HBBHH", 0, 0, 0, 0, 0
    )
    sets = pgs.parse_sup(seg(pgs.SEG_PCS, 9.0, pcs) + seg(pgs.SEG_END, 9.0, b""))
    frames = pgs.frames(sets)
    assert len(frames) == 1
    assert frames[0].start_s == pytest.approx(9.0)
    assert frames[0].duration == pytest.approx(2.0)


def test_clearing_sets_are_not_themselves_cues():
    sets = pgs.parse_sup(
        write_sup(
            [PgsCue(1.0, 2.0, "x")], video_width=320, video_height=240, band_height=48, font_size=20
        )
    )
    assert len(pgs.frames(sets)) == 1


# --------------------------------------------------------------------- raster


def test_render_inverts_so_bright_pgs_text_becomes_dark_on_light():
    """The bug this pins: compositing white text onto a white background.

    Every structural check passed while the rendered image was uniformly blank,
    and OCR simply returned nothing -- which looks exactly like "this file has
    no subtitles". The assertion is that ink exists *and* is darker than paper.
    """
    sets = pgs.parse_sup(
        write_sup([PgsCue(0.0, 1.0, "SHIT")], video_width=640, video_height=360, band_height=96)
    )
    image = pgs.render(sets[0])
    assert image is not None
    darkest, lightest = image.getextrema()
    assert lightest == 255, "background must be paper-white"
    assert darkest < 128, "glyphs must be dark, not invisible"


def test_render_returns_none_for_a_clearing_display_set():
    sets = pgs.parse_sup(
        write_sup([PgsCue(0.0, 1.0, "x")], video_width=320, video_height=240, band_height=48)
    )
    assert pgs.render(sets[1]) is None
