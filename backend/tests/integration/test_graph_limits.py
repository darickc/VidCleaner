"""Feeding real ffmpeg the graphs `graph.py` builds.

The unit tests pin the *format*; these pin the *constants*. If a future ffmpeg
raises or lowers `av_expr_parse`'s 100-term budget, this tier fails instead of
the pipeline silently mis-rendering audio.
"""

from __future__ import annotations

import pytest

from vidcleaner.pipeline.artifacts import TimeRange
from vidcleaner.pipeline.ffmpeg import INAUDIBLE_DB, FFmpegError
from vidcleaner.pipeline.graph import MAX_TERMS_PER_CHUNK, build_mute_graph


def ranges(n: int, step: float = 0.05, width: float = 0.01) -> list[TimeRange]:
    return [TimeRange(start=i * step, end=i * step + width) for i in range(n)]


def run_graph(runner, tmp_path, graph_text: str, name: str = "g") -> None:
    runner.run_filtered(
        input_args=["-f", "lavfi", "-i", "sine=f=1000:d=1"],
        graph=graph_text,
        graph_path=tmp_path / f"{name}.txt",
        output_args=["-map", "[clean]", "-f", "null", "-"],
        label=name,
    )


@pytest.mark.parametrize("count", [0, 1, 89, 90, 91, 200, 1000, 1999])
def test_every_chunk_size_parses(runner, tmp_path, count):
    spec = build_mute_graph(ranges(count), in_label="0:a")
    run_graph(runner, tmp_path, spec.text, f"n{count}")


def test_a_raw_expression_past_the_budget_still_fails(runner, tmp_path):
    """Guards MAX_TERMS_PER_CHUNK: if this ever passes, the chunk size can rise."""
    terms = "+".join(f"between(t,{i * 0.05:.3f},{i * 0.05 + 0.01:.3f})" for i in range(101))
    with pytest.raises(FFmpegError):
        run_graph(runner, tmp_path, f"[0:a]volume=0:enable='{terms}'[clean]", "over")


def test_the_chunk_constant_leaves_headroom_for_the_guard(runner, tmp_path):
    """A full chunk plus the if() wrapper must still parse."""
    spec = build_mute_graph(ranges(MAX_TERMS_PER_CHUNK), in_label="0:a")
    assert spec.n_chunks == 1
    run_graph(runner, tmp_path, spec.text, "full-chunk")


def test_a_chunked_graph_actually_mutes_the_right_ranges(runner, tmp_path, sample_mkv):
    """Two widely separated ranges across two chunks, verified acoustically."""
    targets = [TimeRange(start=1.0, end=2.0), TimeRange(start=7.0, end=8.0)]
    spec = build_mute_graph(targets, in_label="0:a:0", max_terms_per_chunk=1)
    assert spec.n_chunks == 2

    out = tmp_path / "chunked.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=spec.text,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[clean]", "-c:a", "flac", str(out)],
        label="chunked",
    )

    for window in (TimeRange(start=1.2, end=1.8), TimeRange(start=7.2, end=7.8)):
        stats = runner.measure_volume(out, window=window)
        assert stats is not None and stats.max_db <= INAUDIBLE_DB, f"{window} not muted"

    for window in (TimeRange(start=3.0, end=4.0), TimeRange(start=5.0, end=6.0)):
        stats = runner.measure_volume(out, window=window)
        assert stats is not None and stats.max_db > -20.0, f"{window} should be untouched"


def test_boundaries_land_within_sixty_milliseconds(runner, tmp_path, sample_mkv):
    spec = build_mute_graph([TimeRange(start=2.0, end=3.0)], in_label="0:a:0")
    out = tmp_path / "bounded.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=spec.text,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[clean]", "-c:a", "flac", str(out)],
        label="bounded",
    )
    spans = runner.detect_silence(out)
    assert spans
    span = max(spans, key=lambda s: s.end - s.start)
    assert span.start == pytest.approx(2.0, abs=0.06)
    assert span.end == pytest.approx(3.0, abs=0.06)


def test_gated_fades_render_and_leave_the_rest_audible(runner, tmp_path, sample_mkv):
    """The C3 regression, through the real builder rather than a hand-written graph."""
    spec = build_mute_graph(
        [TimeRange(start=2.0, end=3.0), TimeRange(start=5.0, end=5.5)],
        in_label="0:a:0",
        fade_ms=10,
    )
    assert spec.n_fades == 4
    out = tmp_path / "faded.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=spec.text,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[clean]", "-c:a", "flac", str(out)],
        label="faded",
    )
    inside = runner.measure_volume(out, window=TimeRange(start=2.2, end=2.8))
    after = runner.measure_volume(out, window=TimeRange(start=7.0, end=9.0))
    assert inside is not None and after is not None
    assert inside.max_db <= INAUDIBLE_DB
    assert after.max_db > -20.0, "audio after the last fade must survive"


def test_empty_graph_is_a_passthrough(runner, tmp_path, sample_mkv):
    spec = build_mute_graph([], in_label="0:a:0")
    out = tmp_path / "passthrough.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=spec.text,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[clean]", "-c:a", "flac", str(out)],
        label="passthrough",
    )
    stats = runner.measure_volume(out, window=TimeRange(start=2.0, end=3.0))
    assert stats is not None and stats.max_db > -20.0
