"""The ffmpeg subprocess layer, against real media.

These also serve as regression tests for the four PLAN.md §3 corrections: they
assert the observed behaviour, so a future ffmpeg that changes it fails loudly
rather than silently producing wrong audio.
"""

from __future__ import annotations

import pytest

from vidcleaner.pipeline.artifacts import TimeRange
from vidcleaner.pipeline.ffmpeg import (
    INAUDIBLE_DB,
    SILENT_DB,
    FFmpegError,
    get_caps,
    parse_silencedetect,
    parse_volumedetect,
)

MUTE = "[0:a]asetnsamples=n=240:p=0,volume=0[out]"


def test_caps_report_a_modern_ffmpeg():
    caps = get_caps()
    assert caps.version >= (7, 0)
    assert caps.filter_script_flag in {"-/filter_complex", "-filter_complex_script"}
    assert {"aac", "ac3", "eac3", "flac"} <= caps.encoders


def test_the_filter_script_flag_actually_works(runner, tmp_path, sample_mkv):
    """C1: ffmpeg 9 removed -filter_complex_script; -/filter_complex replaces it."""
    out = tmp_path / "out.mkv"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=MUTE,
        graph_path=tmp_path / "graph.txt",
        output_args=["-map", "[out]", "-c:a", "flac", str(out)],
        label="mute",
    )
    assert out.is_file() and out.stat().st_size > 0


def test_graph_is_written_to_disk_not_passed_inline(runner, tmp_path, sample_mkv):
    graph_path = tmp_path / "graph.txt"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=MUTE,
        graph_path=graph_path,
        output_args=["-map", "[out]", "-f", "null", "-"],
        label="mute",
    )
    assert graph_path.read_text().strip() == MUTE


def test_probe_returns_streams_and_format(runner, sample_mkv):
    data = runner.probe(sample_mkv)
    assert data["format"]["format_name"].startswith("matroska")
    kinds = [s["codec_type"] for s in data["streams"]]
    assert kinds.count("audio") == 2
    assert kinds.count("subtitle") == 2
    assert len(data["chapters"]) == 2


def test_probe_raises_for_a_missing_file(runner, tmp_path):
    with pytest.raises(FFmpegError, match="ffprobe"):
        runner.probe(tmp_path / "nope.mkv")


def test_volume_zero_measures_at_the_silence_floor(runner, tmp_path, sample_mkv):
    """PLAN.md §3's -91 dB claim, verified end to end."""
    out = tmp_path / "muted.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=MUTE,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[out]", "-c:a", "flac", str(out)],
        label="mute",
    )
    stats = runner.measure_volume(out)
    assert stats is not None
    assert stats.max_db <= INAUDIBLE_DB
    assert stats.mean_db == pytest.approx(SILENT_DB, abs=1.0)
    assert stats.inaudible


def test_measure_volume_on_an_unmuted_window_is_audible(runner, sample_mkv):
    stats = runner.measure_volume(sample_mkv, window=TimeRange(start=4.0, end=5.0))
    assert stats is not None
    assert stats.max_db > -20.0, "the 1 kHz tone should be loud"


def test_measure_volume_uses_the_last_stats_block(runner, sample_mkv):
    """volumedetect prints n_samples: 0 at configuration time, then for real."""
    stats = runner.measure_volume(sample_mkv, window=TimeRange(start=1.0, end=2.0))
    assert stats is not None and stats.n_samples > 0


def test_decode_check_is_silent_for_good_media(runner, sample_mkv):
    assert runner.decode_check(sample_mkv, stream="0:a:0") == ""


def test_detect_silence_finds_the_muted_range(runner, tmp_path, sample_mkv):
    graph = "[0:a]asetnsamples=n=240:p=0,volume=0:enable='between(t,2.000,3.000)'[out]"
    out = tmp_path / "part.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=graph,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[out]", "-c:a", "flac", str(out)],
        label="mute",
    )
    spans = runner.detect_silence(out)
    assert spans, "expected one silence span"
    span = max(spans, key=lambda s: s.end - s.start)
    assert span.start == pytest.approx(2.0, abs=0.06)
    assert span.end == pytest.approx(3.0, abs=0.06)


def test_progress_callback_receives_monotonic_times(runner, tmp_path, sample_mkv):
    seen: list[float] = []
    runner.run(
        ["-i", str(sample_mkv), "-map", "0:a:0", "-c:a", "flac", str(tmp_path / "a.flac")],
        label="encode",
        on_progress=lambda p: seen.append(p.out_time_s),
        total_duration=10.0,
    )
    assert seen, "no progress blocks parsed"
    assert seen == sorted(seen)
    assert seen[-1] > 0


def test_progress_fraction_is_clamped(runner, tmp_path, sample_mkv):
    fractions: list[float] = []
    runner.run(
        ["-i", str(sample_mkv), "-map", "0:a:0", "-c:a", "flac", str(tmp_path / "a.flac")],
        label="encode",
        on_progress=lambda p: fractions.append(p.fraction) if p.fraction is not None else None,
        total_duration=10.0,
    )
    assert fractions and all(0.0 <= f <= 1.0 for f in fractions)


def test_failure_raises_with_the_log_path(runner, tmp_path):
    with pytest.raises(FFmpegError) as exc:
        runner.run(["-i", str(tmp_path / "nope.mkv"), "-f", "null", "-"], label="bogus")
    assert exc.value.returncode != 0
    assert exc.value.log_path is not None
    assert "full log" in str(exc.value)


def test_the_job_log_reads_as_a_replayable_script(runner, tmp_path, sample_mkv):
    runner.run(["-i", str(sample_mkv), "-map", "0:a:0", "-f", "null", "-"], label="probe-audio")
    text = runner.log_path.read_text()
    assert "==== probe-audio" in text
    assert "$ " in text and "ffmpeg" in text


def test_timeout_kills_the_process(tmp_path, sample_mkv):
    from vidcleaner.pipeline.ffmpeg import FFmpegRunner

    runner = FFmpegRunner(log_path=tmp_path / "ffmpeg.log", default_timeout=0.05)
    with pytest.raises(FFmpegError) as exc:
        runner.run(
            ["-re", "-i", str(sample_mkv), "-c:a", "flac", str(tmp_path / "slow.flac")],
            label="slow",
        )
    assert exc.value.timed_out is True


# ------------------------------------------------------------------- C2 / C3


@pytest.mark.parametrize("count", [1, 50, 90, 100])
def test_expression_terms_up_to_the_budget_parse(runner, tmp_path, sample_mkv, count):
    """C2: av_expr_parse has a hard depth budget of 100 `+` terms."""
    terms = "+".join(f"between(t,{i * 0.05:.3f},{i * 0.05 + 0.01:.3f})" for i in range(count))
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=f"[0:a]asetnsamples=n=240:p=0,volume=0:enable='{terms}'[out]",
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[out]", "-f", "null", "-"],
        label="terms",
    )


def test_one_term_past_the_budget_fails(runner, tmp_path, sample_mkv):
    """The guard on the chunking constant: if this ever passes, raise the chunk size."""
    terms = "+".join(f"between(t,{i * 0.05:.3f},{i * 0.05 + 0.01:.3f})" for i in range(101))
    with pytest.raises(FFmpegError):
        runner.run_filtered(
            input_args=["-i", str(sample_mkv)],
            graph=f"[0:a]asetnsamples=n=240:p=0,volume=0:enable='{terms}'[out]",
            graph_path=tmp_path / "g.txt",
            output_args=["-map", "[out]", "-f", "null", "-"],
            label="terms",
        )


def test_ungated_afade_silences_the_whole_stream(runner, tmp_path, sample_mkv):
    """C3, the reason every afade must carry its own `enable`.

    Without gating, afade=t=out holds its output at zero for the rest of the
    stream, so a chain of out/in pairs mutes the entire file -- and every
    structural check still passes. This asserts the broken behaviour so the
    gated version below is demonstrably the fix, not a superstition.
    """
    graph = (
        "[0:a]afade=t=out:st=1.99:d=0.01:curve=tri,"
        "afade=t=in:st=3:d=0.01:curve=tri,"
        "volume=0:enable='between(t,2.000,3.000)'[out]"
    )
    out = tmp_path / "ungated.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=graph,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[out]", "-c:a", "flac", str(out)],
        label="ungated",
    )
    stats = runner.measure_volume(out, window=TimeRange(start=6.0, end=8.0))
    assert stats is not None
    assert stats.max_db <= INAUDIBLE_DB, "expected the known-broken whole-file mute"


def test_gated_afade_only_affects_its_own_range(runner, tmp_path, sample_mkv):
    graph = (
        "[0:a]afade=t=out:st=1.99:d=0.01:curve=tri:enable='between(t,1.99,2.00)',"
        "afade=t=in:st=3:d=0.01:curve=tri:enable='between(t,3.00,3.01)',"
        "volume=0:enable='between(t,2.000,3.000)'[out]"
    )
    out = tmp_path / "gated.flac"
    runner.run_filtered(
        input_args=["-i", str(sample_mkv)],
        graph=graph,
        graph_path=tmp_path / "g.txt",
        output_args=["-map", "[out]", "-c:a", "flac", str(out)],
        label="gated",
    )
    inside = runner.measure_volume(out, window=TimeRange(start=2.2, end=2.8))
    outside = runner.measure_volume(out, window=TimeRange(start=6.0, end=8.0))
    assert inside is not None and outside is not None
    assert inside.max_db <= INAUDIBLE_DB
    assert outside.max_db > -20.0, "audio outside the range must be untouched"


def test_asetnsamples_tightens_the_mute_boundary(runner, tmp_path, sample_mkv):
    """C6/§3: `enable` gates whole frames, so 5 ms reframing is required."""

    def boundary(graph: str, name: str) -> float:
        out = tmp_path / name
        runner.run_filtered(
            input_args=["-i", str(sample_mkv)],
            graph=graph,
            graph_path=tmp_path / f"{name}.txt",
            output_args=["-map", "[out]", "-c:a", "flac", str(out)],
            label=name,
        )
        spans = runner.detect_silence(out)
        assert spans
        return max(spans, key=lambda s: s.end - s.start).start

    coarse = boundary("[0:a]volume=0:enable='between(t,2.000,3.000)'[out]", "coarse.flac")
    fine = boundary(
        "[0:a]asetnsamples=n=240:p=0,volume=0:enable='between(t,2.000,3.000)'[out]", "fine.flac"
    )
    assert abs(fine - 2.0) <= abs(coarse - 2.0)
    assert abs(fine - 2.0) < 0.010


# -------------------------------------------------------------------- parsers


def test_parse_volumedetect_skips_the_configuration_block():
    stderr = (
        "[Parsed_volumedetect_2] n_samples: 0\n"
        "[Parsed_volumedetect_2] n_samples: 88200\n"
        "[Parsed_volumedetect_2] mean_volume: -91.0 dB\n"
        "[Parsed_volumedetect_2] max_volume: -91.0 dB\n"
    )
    stats = parse_volumedetect(stderr)
    assert stats is not None
    assert stats.n_samples == 88200
    assert stats.mean_db == -91.0
    assert stats.max_db == -91.0


def test_parse_volumedetect_returns_none_without_stats():
    assert parse_volumedetect("nothing here") is None


def test_parse_silencedetect_pairs_starts_and_ends():
    stderr = (
        "[silencedetect] silence_start: 1.001\n"
        "[silencedetect] silence_end: 2.002 | silence_duration: 1.001\n"
        "[silencedetect] silence_start: 4.0\n"
        "[silencedetect] silence_end: 4.5 | silence_duration: 0.5\n"
    )
    spans = parse_silencedetect(stderr)
    assert [(round(s.start, 3), round(s.end, 3)) for s in spans] == [(1.001, 2.002), (4.0, 4.5)]
