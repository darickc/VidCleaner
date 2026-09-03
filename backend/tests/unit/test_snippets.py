"""Snippet windowing and graph construction (§6 step 10). No ffmpeg."""

from __future__ import annotations

import re

import pytest

from vidcleaner.pipeline.snippets import WINDOW_S, clip_window, snippet_graph


def test_the_window_is_centred_on_the_mute() -> None:
    start, length, rel_start, rel_end = clip_window(10.0, 10.4)
    assert length == WINDOW_S
    assert start == 10.2 - WINDOW_S / 2
    assert (rel_start + rel_end) / 2 == pytest.approx(WINDOW_S / 2)


def test_a_hit_at_the_start_still_gets_a_full_window() -> None:
    """Clamped, not centred: two seconds of context is better than 0.5 s."""
    start, length, rel_start, rel_end = clip_window(0.2, 0.5)
    assert start == 0.0
    assert length == WINDOW_S
    assert rel_start == 0.2 and rel_end == 0.5


def test_a_hit_at_the_end_is_pulled_back_inside_the_file() -> None:
    start, length, rel_start, _ = clip_window(99.5, 99.9, duration_s=100.0)
    assert start == 95.0
    assert length == WINDOW_S
    assert rel_start == pytest.approx(4.5)


def test_a_file_shorter_than_the_window_is_not_over_read() -> None:
    start, length, _, _ = clip_window(1.0, 1.2, duration_s=3.0)
    assert start == 0.0
    assert length == 3.0


def test_a_mute_longer_than_the_window_is_not_cropped() -> None:
    """A merged run of swearing can exceed 5 s; the clip grows to contain it."""
    _, length, rel_start, rel_end = clip_window(10.0, 18.0)
    assert length == 8.0
    assert (rel_start, rel_end) == (0.0, 8.0)


def test_the_audio_offset_is_subtracted_exactly_once() -> None:
    """ONE CLOCK: detections are container time, audio.wav is 0-based."""
    plain = clip_window(10.0, 10.4)
    shifted = clip_window(11.4, 11.8, audio_offset_s=1.4)
    assert shifted == plain


def test_the_graph_mutes_only_the_detection_range() -> None:
    graph = snippet_graph(2.25, 2.75, 5.0)
    assert "volume=0:enable='between(t,2.250,2.750)'" in graph
    assert "asetpts=PTS-STARTPTS" in graph, "the enable expression needs a 0-based clock"
    assert "asetnsamples=n=240" in graph, "frame-quantised enable, as in render.py"


def test_the_graph_produces_all_three_outputs() -> None:
    graph = snippet_graph(0.0, 0.5, 5.0)
    assert set(re.findall(r"\[(orig|clean|wave)\]", graph)) == {"orig", "clean", "wave"}


def test_the_highlight_box_tracks_the_muted_span() -> None:
    graph = snippet_graph(2.5, 3.0, 5.0)
    box = re.search(r"drawbox=x=(\d+):y=0:w=(\d+)", graph)
    assert box is not None
    assert int(box.group(1)) == 320, "half way across a 640 px waveform"
    assert int(box.group(2)) == 64, "0.5 s of 5 s"


def test_a_very_short_mute_still_draws_a_visible_box() -> None:
    box = re.search(r"drawbox=x=\d+:y=0:w=(\d+)", snippet_graph(2.5, 2.505, 5.0))
    assert box is not None and int(box.group(1)) >= 2
