"""Batched whisperX alignment and the full-pass progress model (PLAN.md §6 step 4).

All of this is pure functions over plain dicts, so it runs with no torch and no
media -- which is the point: the behaviour being pinned here only misbehaves on
a two-hour file that no test tier can afford to produce.
"""

from __future__ import annotations

from vidcleaner.pipeline.artifacts import TimeRange
from vidcleaner.pipeline.stt import TranscribeRequest
from vidcleaner.pipeline.whisper_backend import (
    ALIGN_CHUNK_S,
    _bucket_for,
    _chunk_segments,
    _merge_alignment,
)


def seg(start: float, end: float, text: str = "", words=None) -> dict:
    return {"start": start, "end": end, "text": text, "words": words or []}


def word(w: str, start: float, end: float, score: float | None = 0.9) -> dict:
    return {"word": w, "start": start, "end": end, "score": score}


# ------------------------------------------------------------------ chunking


def test_short_input_is_a_single_batch():
    batches = _chunk_segments([seg(0, 10), seg(10, 20)])
    assert len(batches) == 1
    assert len(batches[0]) == 2


def test_batches_are_split_on_span():
    segments = [seg(i * 60.0, i * 60.0 + 60.0) for i in range(20)]  # 20 minutes
    batches = _chunk_segments(segments, max_span_s=300.0)
    assert len(batches) == 4
    for batch in batches:
        span = batch[-1]["end"] - batch[0]["start"]
        assert span <= 300.0


def test_batches_are_split_on_count():
    """Thousands of sub-second segments fit inside one span but not one call."""
    segments = [seg(i * 0.1, i * 0.1 + 0.1) for i in range(500)]
    batches = _chunk_segments(segments, max_span_s=1e9, max_segments=128)
    assert [len(b) for b in batches] == [128, 128, 128, 116]


def test_chunking_preserves_order_and_loses_nothing():
    segments = [seg(i * 30.0, i * 30.0 + 30.0, text=str(i)) for i in range(40)]
    flat = [s for batch in _chunk_segments(segments) for s in batch]
    assert flat == segments


def test_a_two_hour_pass_is_many_batches_not_one():
    """The defect this exists to prevent: one align call holding the whole file."""
    segments = [seg(i * 5.0, i * 5.0 + 5.0) for i in range(1440)]  # 2 hours
    batches = _chunk_segments(segments)
    assert len(batches) > 20
    assert max(b[-1]["end"] - b[0]["start"] for b in batches) <= ALIGN_CHUNK_S


def test_empty_input_is_no_batches():
    assert _chunk_segments([]) == []


# ------------------------------------------------------------------- merging


def test_alignment_keeps_the_original_segmentation():
    """The old implementation collapsed every segment into one. On a full pass
    that produced a single segment of ~20,000 words with all text concatenated.
    """
    raw = [
        seg(0.0, 2.0, "hello there", [word("hello", 0.0, 0.5)]),
        seg(10.0, 12.0, "goodbye now", [word("goodbye", 10.0, 10.5)]),
    ]
    aligned = {
        "segments": [
            {"words": [word("hello", 0.1, 0.6), word("there", 0.7, 1.2)]},
            {"words": [word("goodbye", 10.1, 10.6), word("now", 10.8, 11.2)]},
        ]
    }
    out = _merge_alignment(raw, aligned)

    assert len(out) == 2, "segmentation must survive alignment"
    assert out[0]["text"] == "hello there"
    assert out[1]["text"] == "goodbye now"
    assert [w["word"] for w in out[0]["words"]] == ["hello", "there"]
    assert [w["word"] for w in out[1]["words"]] == ["goodbye", "now"]
    assert all(w["aligned"] for s in out for w in s["words"])


def test_words_are_placed_by_midpoint_not_by_order():
    raw = [seg(0.0, 5.0, "a", [word("a", 0.0, 1.0)]), seg(5.0, 10.0, "b", [word("b", 5.0, 6.0)])]
    aligned = {"segments": [{"words": [word("b", 7.0, 7.5), word("a", 1.0, 1.5)]}]}
    out = _merge_alignment(raw, aligned)
    assert [w["word"] for w in out[0]["words"]] == ["a"]
    assert [w["word"] for w in out[1]["words"]] == ["b"]


def test_a_segment_with_no_aligned_word_keeps_its_native_timing():
    raw = [
        seg(0.0, 2.0, "hello", [word("hello", 0.0, 0.5)]),
        seg(10.0, 12.0, "42", [word("42", 10.0, 10.4)]),
    ]
    aligned = {"segments": [{"words": [word("hello", 0.1, 0.6)]}]}
    out = _merge_alignment(raw, aligned)
    assert out[1]["words"][0]["start"] == 10.0
    assert not out[1]["words"][0].get("aligned")


def test_unalignable_words_are_skipped_not_crashed_on():
    raw = [seg(0.0, 2.0, "x", [word("x", 0.0, 0.5)])]
    aligned = {"segments": [{"words": [{"word": "x", "start": None, "end": None}]}]}
    assert _merge_alignment(raw, aligned) == list(raw)


def test_an_empty_alignment_falls_back_to_the_native_timings():
    raw = [seg(0.0, 2.0, "x", [word("x", 0.0, 0.5)])]
    assert _merge_alignment(raw, {"segments": []}) == list(raw)


def test_a_word_nudged_outside_its_segment_goes_to_the_nearest():
    bounds = [(0.0, 5.0), (10.0, 15.0)]
    assert _bucket_for(2.0, bounds) == 0
    assert _bucket_for(12.0, bounds) == 1
    assert _bucket_for(5.4, bounds) == 0  # just past the end of the first
    assert _bucket_for(9.6, bounds) == 1  # just before the start of the second


# ------------------------------------------------------------------ progress


def test_a_full_pass_measures_progress_by_position():
    """VAD emits no segments for silence, so accumulated duration under-reports.

    A film that is 40% silence would stall the bar at 60% and never finish.
    """
    request = TranscribeRequest(audio_path="a.wav", windows=[], duration_s=1000.0)
    assert not request.windows
    assert request.progress_total_s == 1000.0


def test_a_windowed_pass_measures_against_the_window_total():
    request = TranscribeRequest(
        audio_path="a.wav",
        windows=[TimeRange(start=0.0, end=10.0), TimeRange(start=100.0, end=110.0)],
        duration_s=1000.0,
    )
    assert request.progress_total_s == 20.0
