"""The mute filter graph builder. Pure -- golden strings, no ffmpeg.

The companion integration test (`test_graph_limits.py`) feeds these graphs to a
real ffmpeg, which is what pins the 90-term chunk constant.
"""

from __future__ import annotations

import pytest

from vidcleaner.pipeline.artifacts import TimeRange
from vidcleaner.pipeline.graph import (
    MAX_FADE_RANGES,
    MAX_RANGES,
    MAX_TERMS_PER_CHUNK,
    GraphTooLarge,
    build_mute_graph,
)

HEAD = "[0:a:0]asetnsamples=n=240:p=0"


def r(start: float, end: float) -> TimeRange:
    return TimeRange(start=start, end=end)


def ranges(n: int, step: float = 0.05, width: float = 0.01) -> list[TimeRange]:
    return [r(i * step, i * step + width) for i in range(n)]


# ------------------------------------------------------------------- goldens


def test_empty_range_list_is_a_passthrough():
    """Not an empty volume filter: `enable=''` would gate nothing predictably."""
    spec = build_mute_graph([])
    assert spec.text == "[0:a:0]anull[clean]"
    assert (spec.n_ranges, spec.n_chunks, spec.n_fades) == (0, 0, 0)


def test_single_range_golden():
    spec = build_mute_graph([r(1.0, 2.0)])
    assert spec.text == (
        HEAD + ",volume=0:enable='if(between(t,1.000,2.000),between(t,1.000,2.000),0)'[clean]"
    )


def test_two_ranges_golden():
    spec = build_mute_graph([r(1.2344, 1.5666), r(4.0, 4.25)])
    assert spec.text == (
        HEAD + ",volume=0:enable='if(between(t,1.234,4.250),"
        "between(t,1.234,1.567)+between(t,4.000,4.250),0)'[clean]"
    )


def test_fades_golden():
    spec = build_mute_graph([r(2.0, 3.0)], fade_ms=10)
    assert spec.text == (
        HEAD + ",afade=t=out:st=1.990:d=0.010:curve=tri:enable='between(t,1.990,2.000)'"
        ",afade=t=in:st=3.000:d=0.010:curve=tri:enable='between(t,3.000,3.010)'"
        ",volume=0:enable='if(between(t,2.000,3.000),between(t,2.000,3.000),0)'[clean]"
    )
    assert spec.n_fades == 2


def test_labels_are_configurable():
    spec = build_mute_graph([r(1.0, 2.0)], in_label="0:a", out_label="out")
    assert spec.text.startswith("[0:a]")
    assert spec.text.endswith("[out]")
    assert spec.out_label == "out"


# ------------------------------------------------------------------ rounding


def test_times_are_rounded_outward():
    """Quantization may lengthen a mute, never leak a syllable."""
    spec = build_mute_graph([r(1.23449, 1.23451)])
    assert "between(t,1.234,1.235)" in spec.text


def test_sub_millisecond_range_survives_as_one_millisecond():
    spec = build_mute_graph([r(0.0004, 0.0006)])
    assert "between(t,0.000,0.001)" in spec.text


def test_no_scientific_notation_anywhere():
    spec = build_mute_graph([r(0.0000001, 1e-6 + 0.5)])
    assert "e-" not in spec.text and "e+" not in spec.text


def test_graph_text_is_pure_ascii():
    build_mute_graph(ranges(50)).text.encode("ascii")


# --------------------------------------------------------------- normalizing


def test_inverted_and_empty_ranges_are_dropped():
    spec = build_mute_graph([r(5.0, 1.0), r(2.0, 2.0), r(3.0, 4.0)])
    assert spec.n_ranges == 1
    assert "between(t,3.000,4.000)" in spec.text


def test_ranges_are_clamped_to_the_duration():
    spec = build_mute_graph([r(9.0, 15.0)], duration=10.0)
    assert "between(t,9.000,10.000)" in spec.text


def test_ranges_past_the_duration_are_dropped_entirely():
    assert build_mute_graph([r(20.0, 25.0)], duration=10.0).n_ranges == 0


def test_negative_start_is_clamped_to_zero():
    spec = build_mute_graph([r(-1.0, 0.5)])
    assert "between(t,0.000,0.500)" in spec.text


def test_overlapping_ranges_are_coalesced():
    spec = build_mute_graph([r(1.0, 2.0), r(1.5, 3.0)])
    assert spec.n_ranges == 1
    assert "between(t,1.000,3.000)" in spec.text


def test_unsorted_input_is_ordered():
    spec = build_mute_graph([r(5.0, 6.0), r(1.0, 2.0), r(3.0, 4.0)])
    positions = [spec.text.index(f"between(t,{v}") for v in ("1.000", "3.000", "5.000")]
    assert positions == sorted(positions)


def test_builder_is_idempotent_under_reordering():
    forward = build_mute_graph([r(1.0, 2.0), r(3.0, 4.0), r(5.0, 6.0)])
    backward = build_mute_graph([r(5.0, 6.0), r(3.0, 4.0), r(1.0, 2.0)])
    assert forward.text == backward.text


# ---------------------------------------------------------------- chunking


@pytest.mark.parametrize(
    ("count", "expected_chunks"),
    [(1, 1), (89, 1), (90, 1), (91, 2), (180, 2), (181, 3), (900, 10)],
)
def test_chunk_count(count, expected_chunks):
    assert build_mute_graph(ranges(count)).n_chunks == expected_chunks


def test_chunk_boundary_at_ninety_is_one_filter():
    spec = build_mute_graph(ranges(90))
    assert spec.text.count("volume=0:enable=") == 1


def test_chunk_boundary_at_ninety_one_splits():
    spec = build_mute_graph(ranges(91))
    assert spec.text.count("volume=0:enable=") == 2


@pytest.mark.parametrize("count", [1, 90, 91, 200, 1000])
def test_no_chunk_exceeds_the_term_budget(count):
    """The measured ffmpeg limit is 100 `+` terms; the guard adds one more."""
    spec = build_mute_graph(ranges(count))
    for chunk in spec.text.split("volume=0:enable='")[1:]:
        expression = chunk.split("'")[0]
        assert expression.count("between(") <= MAX_TERMS_PER_CHUNK + 1


def test_chunk_size_is_configurable():
    spec = build_mute_graph(ranges(10), max_terms_per_chunk=3)
    assert spec.n_chunks == 4


def test_every_range_appears_exactly_once():
    spec = build_mute_graph(ranges(200))
    assert spec.text.count("between(t,") == spec.n_ranges + spec.n_chunks


# ------------------------------------------------------------------- guards


def test_too_many_ranges_is_a_hard_error():
    with pytest.raises(GraphTooLarge, match="detector fault"):
        build_mute_graph(ranges(MAX_RANGES + 1))


def test_the_range_guard_boundary_is_allowed():
    assert build_mute_graph(ranges(MAX_RANGES)).n_ranges == MAX_RANGES


def test_fades_are_dropped_above_the_limit_with_a_warning():
    spec = build_mute_graph(ranges(MAX_FADE_RANGES + 1), fade_ms=10)
    assert spec.n_fades == 0
    assert any("fades_dropped" in w for w in spec.warnings)
    assert spec.n_ranges > 0, "the mute itself must be unaffected"


def test_fades_at_the_limit_are_kept():
    spec = build_mute_graph(ranges(MAX_FADE_RANGES), fade_ms=10)
    assert spec.n_fades > 0


def test_no_fades_when_fade_ms_is_zero():
    spec = build_mute_graph([r(1.0, 2.0)], fade_ms=0)
    assert "afade" not in spec.text and spec.n_fades == 0


def test_fade_out_is_shortened_to_the_available_lead():
    """With only 2 ms before the range, fade for 2 ms rather than dropping it."""
    spec = build_mute_graph([r(0.002, 1.0)], fade_ms=10)
    assert "afade=t=out:st=0.000:d=0.002" in spec.text
    assert "afade=t=in:st=1.000:d=0.010" in spec.text
    assert spec.n_fades == 2


def test_fade_out_is_skipped_for_a_range_starting_at_zero():
    """At t=0 there is no room at all, so only the fade-in is emitted."""
    spec = build_mute_graph([r(0.0, 1.0)], fade_ms=10)
    assert "afade=t=out" not in spec.text
    assert "afade=t=in" in spec.text
    assert spec.n_fades == 1


def test_every_fade_carries_its_own_enable():
    """An ungated afade=t=out silences the rest of the stream (measured)."""
    spec = build_mute_graph(ranges(5, step=1.0, width=0.2), fade_ms=10)
    fades = [f for f in spec.text.split(",") if f.startswith("afade")]
    assert fades
    assert all("enable=" in f for f in fades)


def test_asetnsamples_always_disables_padding():
    """`pad` defaults to true and would append up to 239 samples of silence."""
    assert ":p=0" in build_mute_graph([r(1.0, 2.0)]).text


def test_frame_samples_is_configurable():
    assert "asetnsamples=n=480:p=0" in build_mute_graph([r(1.0, 2.0)], frame_samples=480).text
