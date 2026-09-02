"""Full render and verify against real media -- PLAN.md §12's core assertions."""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.matching.compiler import build_matcher
from vidcleaner.pipeline import probe as probe_stage
from vidcleaner.pipeline import render as render_stage
from vidcleaner.pipeline import verify as verify_stage
from vidcleaner.pipeline.artifacts import (
    Detection,
    DetectionResult,
    ProfileSnapshot,
    TimeRange,
)
from vidcleaner.pipeline.ffmpeg import INAUDIBLE_DB, FFmpegRunner
from vidcleaner.pipeline.stages import StageError, build_context, build_spec, run_stage
from vidcleaner.settings_store import AppSettings

MUTE_A = TimeRange(start=2.0, end=3.0)
MUTE_B = TimeRange(start=7.0, end=7.6)


def detections_for(*ranges: TimeRange) -> DetectionResult:
    """Hand-built detections, so render/verify never need the STT stack."""
    return DetectionResult(
        profile_hash="v1:test",
        detections=[
            Detection(
                word_raw="shit",
                word_canonical="shit",
                category="strong",
                start_s=r.start,
                end_s=r.end,
                mute_start_s=r.start,
                mute_end_s=r.end,
                source="both",
                confidence=0.95,
            )
            for r in ranges
        ],
        mute_ranges=list(ranges),
        total_muted_s=sum(r.duration for r in ranges),
    )


@pytest.fixture
def rendered(settings, sample_mkv):
    """Run probe -> subtitles -> (injected detections) -> render -> verify."""

    def go(source: Path = None, *, ranges=(MUTE_A, MUTE_B), **settings_kw):
        src = source or sample_mkv
        # Drift is measured in its own tests; running a real `small` pass on a
        # sine-tone fixture in every render test cost ~1 s each for nothing.
        spec = build_spec(
            src,
            profile=ProfileSnapshot(profile_hash="v1:test"),
            settings=AppSettings(**{"drift_check": False, **settings_kw}),
        )
        ctx = build_context(spec, deploy=settings)
        ctx.matcher = build_matcher()
        for stage in ("probe", "extract", "subtitles"):
            run_stage(ctx, stage)
        detections_for(*ranges).write(ctx.ws.detections_json)
        ctx.ws.mark_done("detect")
        run_stage(ctx, "render")
        return ctx

    return go


# ------------------------------------------------------------------- layout


def test_output_has_the_clean_track_first(rendered):
    ctx = rendered()
    out = probe_stage.load(ctx.ws)
    result = render_stage.load(ctx.ws)
    data = FFmpegRunner().probe(Path(result.out_path))

    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == len(out.audio) + 1
    assert audio[0]["tags"]["title"] == "Clean"
    assert audio[0]["disposition"]["default"] == 1


def test_video_stays_at_absolute_index_zero(rendered):
    ctx = rendered()
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    assert data["streams"][0]["codec_type"] == "video"
    assert data["streams"][1]["codec_type"] == "audio"


def test_original_track_is_titled_and_not_default(rendered):
    ctx = rendered()
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    assert audio[1]["tags"]["title"] == "Original"
    assert audio[1]["disposition"]["default"] == 0


def test_exactly_one_default_audio_track(rendered):
    ctx = rendered()
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    assert sum(s["disposition"]["default"] for s in audio) == 1


def test_clearing_default_preserves_other_dispositions(rendered):
    """C4: a literal `-disposition:a:N 0` would wipe `comment` here."""
    ctx = rendered()
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    commentary = audio[2]
    assert commentary["disposition"]["comment"] == 1
    assert commentary["disposition"]["default"] == 0


def test_clean_track_mirrors_the_source_language_and_channels(rendered):
    ctx = rendered()
    src = probe_stage.load(ctx.ws)
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    clean = [s for s in data["streams"] if s["codec_type"] == "audio"][0]
    assert clean["tags"]["language"] == src.source_audio.language
    assert int(clean["channels"]) == src.source_audio.channels


def test_clean_track_uses_the_planned_encoder(rendered):
    ctx = rendered()
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    clean = [s for s in data["streams"] if s["codec_type"] == "audio"][0]
    assert clean["codec_name"] == "ac3"


def test_subtitles_and_chapters_survive(rendered):
    ctx = rendered()
    src = probe_stage.load(ctx.ws)
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    subs = [s for s in data["streams"] if s["codec_type"] == "subtitle"]
    assert len(subs) == len(src.subtitles)
    assert len(data["chapters"]) == src.chapter_count == 2


def test_idempotency_tags_are_written(rendered):
    ctx = rendered()
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    tags = {k.upper(): v for k, v in data["format"]["tags"].items()}
    assert tags["VIDCLEANER"] == "1"
    assert tags["VIDCLEANER_PROFILE_HASH"] == "v1:test"
    assert tags["VIDCLEANER_JOB"] == ctx.spec.job_id
    assert tags["VIDCLEANER_SRC_FP"] == probe_stage.load(ctx.ws).fingerprint


def test_a_second_probe_reports_already_clean(rendered):
    """The §4 idempotency loop closes: our own output is recognised."""
    ctx = rendered()
    out = Path(render_stage.load(ctx.ws).out_path)
    runner = FFmpegRunner()
    stat = out.stat()
    again = probe_stage.parse_probe(
        runner.probe(out),
        path=out,
        size=stat.st_size,
        mtime=stat.st_mtime,
        profile_hash="v1:test",
    )
    assert again.already_clean is True


# ----------------------------------------------------------------- acoustic


def test_the_requested_ranges_are_silent(rendered, runner):
    ctx = rendered()
    out = Path(render_stage.load(ctx.ws).out_path)
    for window in (TimeRange(start=2.2, end=2.8), TimeRange(start=7.2, end=7.5)):
        stats = runner.measure_volume(out, window=window)
        assert stats is not None
        assert stats.max_db <= INAUDIBLE_DB, f"{window} peaks at {stats.max_db}"


def test_audio_outside_the_ranges_is_untouched(rendered, runner):
    ctx = rendered()
    out = Path(render_stage.load(ctx.ws).out_path)
    for window in (TimeRange(start=0.5, end=1.5), TimeRange(start=4.0, end=6.0)):
        stats = runner.measure_volume(out, window=window)
        assert stats is not None
        assert stats.max_db > -20.0, f"{window} should still be audible"


def test_mute_boundaries_land_within_sixty_milliseconds(rendered, runner):
    ctx = rendered()
    out = Path(render_stage.load(ctx.ws).out_path)
    spans = runner.detect_silence(out)
    longest = max(spans, key=lambda s: s.end - s.start)
    assert longest.start == pytest.approx(MUTE_A.start, abs=0.06)
    assert longest.end == pytest.approx(MUTE_A.end, abs=0.06)


def test_the_original_track_is_not_muted(rendered, runner):
    """Only a:0 is filtered; a:1 must be a byte-for-byte copy."""
    ctx = rendered()
    out = Path(render_stage.load(ctx.ws).out_path)
    stats = runner.measure_volume(out, stream="0:a:1", window=TimeRange(start=2.2, end=2.8))
    assert stats is not None
    assert stats.max_db > -20.0


# ----------------------------------------------------------------- verify


def test_verify_passes_on_a_good_render(rendered):
    ctx = rendered()
    run_stage(ctx, "verify")
    result = verify_stage.load(ctx.ws)
    assert result is not None and result.ok
    assert not result.failures
    assert result.measured_db


def test_verify_measures_both_silence_and_a_control_window(rendered):
    ctx = rendered()
    run_stage(ctx, "verify")
    result = verify_stage.load(ctx.ws)
    names = {c.name for c in result.checks}
    assert "control_window_audible" in names
    assert any(n.startswith("mute_window_silent") for n in names)
    assert next(c for c in result.checks if c.name == "control_window_audible").ok


def test_verify_catches_a_wholly_silenced_output(rendered, runner, tmp_path):
    """The control window is the only check that catches the afade catastrophe."""
    ctx = rendered()
    out = Path(render_stage.load(ctx.ws).out_path)
    broken = tmp_path / "broken.mkv"
    runner.run_filtered(
        input_args=["-i", str(out)],
        graph="[0:a:0]volume=0[silent]",
        graph_path=tmp_path / "g.txt",
        output_args=[
            "-map",
            "0:V",
            "-map",
            "[silent]",
            "-map",
            "0:a",
            "-map",
            "0:s?",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "copy",
            "-c:a:0",
            "ac3",
            "-disposition:a:0",
            "default",
            "-metadata:s:a:0",
            "title=Clean",
            "-metadata:s:a:0",
            "language=eng",
            "-f",
            "matroska",
            str(broken),
        ],
        label="silence-everything",
    )

    from vidcleaner.pipeline.artifacts import DetectionResult as DR
    from vidcleaner.pipeline.render import plan_render

    src = probe_stage.load(ctx.ws)
    det = DR.read(ctx.ws.detections_json)
    plan = plan_render(src, det, output=broken, job_id="j", profile_hash="v1:test")
    stat = broken.stat()
    out_probe = probe_stage.parse_probe(
        runner.probe(broken), path=broken, size=stat.st_size, mtime=stat.st_mtime
    )
    result = verify_stage.verify_render(
        runner,
        source=src,
        plan=plan,
        mute_ranges=det.mute_ranges,
        output_probe=out_probe,
        output_path=broken,
    )
    control = next(c for c in result.checks if c.name == "control_window_audible")
    assert not control.ok, "a fully silenced file must fail the control window"
    assert not result.ok


def test_verify_rejects_a_truncated_output(rendered, runner, tmp_path):
    ctx = rendered()
    src = probe_stage.load(ctx.ws)
    short = tmp_path / "short.mkv"
    runner.run(
        ["-t", "3", "-i", str(render_stage.load(ctx.ws).out_path), "-c", "copy", str(short)],
        label="truncate",
    )
    from vidcleaner.pipeline.artifacts import DetectionResult as DR
    from vidcleaner.pipeline.render import plan_render

    det = DR.read(ctx.ws.detections_json)
    plan = plan_render(src, det, output=short, job_id="j", profile_hash="v1:test")
    stat = short.stat()
    out_probe = probe_stage.parse_probe(
        runner.probe(short), path=short, size=stat.st_size, mtime=stat.st_mtime
    )
    result = verify_stage.verify_render(
        runner,
        source=src,
        plan=plan,
        mute_ranges=det.mute_ranges,
        output_probe=out_probe,
        output_path=short,
    )
    failed = {c.name for c in result.failures}
    assert "duration_within_tolerance" in failed
    assert "output_size_floor" in failed
    assert not result.ok


def test_verify_stage_raises_on_failure(rendered, tmp_path):
    ctx = rendered()
    # Point the render result at a deliberately wrong file.
    result = render_stage.load(ctx.ws)
    bogus = ctx.ws.root / "bogus.mkv"
    bogus.write_bytes(b"not a matroska file")
    result.model_copy(update={"out_path": str(bogus)}).write(ctx.ws.render_json)
    with pytest.raises(StageError):
        run_stage(ctx, "verify")


# ---------------------------------------------------------------- redaction


def test_the_english_subtitle_is_redacted_in_the_output(rendered, runner, tmp_path):
    ctx = rendered()
    result = render_stage.load(ctx.ws)
    assert result.redacted, "the English stream should have been redacted"

    extracted = tmp_path / "out.srt"
    runner.run(
        ["-i", result.out_path, "-map", "0:s:0", "-c:s", "srt", str(extracted)],
        label="extract-check",
    )
    text = extracted.read_text()
    assert "****" in text
    assert "Scunthorpe" in text, "never-match words stay intact"
    assert "fucking" not in text.lower()


def test_the_spanish_subtitle_is_untouched(rendered, runner, tmp_path):
    ctx = rendered()
    extracted = tmp_path / "es.srt"
    runner.run(
        ["-i", render_stage.load(ctx.ws).out_path, "-map", "0:s:1", "-c:s", "srt", str(extracted)],
        label="extract-es",
    )
    assert "mierda" in extracted.read_text(), "no Spanish word list, so no redaction"


def test_redaction_can_be_disabled(rendered):
    ctx = rendered(redact_subtitles=False)
    assert render_stage.load(ctx.ws).redacted == []


# --------------------------------------------------------- sidecar redaction


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_a_sidecar_is_redacted_into_work_and_not_the_library(rendered, sample_mkv):
    """M1's log deferred installing redacted sidecars to swap.py, but nothing
    ever *produced* them: `subs.redactable` is built from embedded streams only."""
    sidecar = sample_mkv.with_suffix(".srt")
    sidecar.write_text((FIXTURES / "marked.srt").read_text())
    original = sidecar.read_text()

    ctx = rendered(sample_mkv)
    result = render_stage.load(ctx.ws)
    produced = [r for r in result.redacted if r.sidecar_source]
    assert len(produced) == 1
    assert produced[0].sidecar_source == str(sidecar)
    assert produced[0].replacements > 0

    staged = Path(produced[0].output_path)
    assert staged.is_dir() is False and staged.is_file()
    assert ctx.ws.redacted_dir in staged.parents
    assert "****" in staged.read_text()
    assert "fucking" not in staged.read_text().lower()
    # The library file is untouched: only swap.py may write there.
    assert sidecar.read_text() == original


def test_a_sidecar_in_another_language_is_left_alone(rendered, sample_mkv):
    sidecar = sample_mkv.parent / f"{sample_mkv.stem}.es.srt"
    sidecar.write_text((FIXTURES / "marked.es.srt").read_text())
    ctx = rendered(sample_mkv)
    assert [r for r in render_stage.load(ctx.ws).redacted if r.sidecar_source] == []


def test_a_sidecar_with_no_hits_produces_nothing_to_install(rendered, sample_mkv):
    """Replacing a library file with a byte-different copy of itself is worse
    than leaving it alone."""
    sidecar = sample_mkv.with_suffix(".srt")
    sidecar.write_text("1\n00:00:01,000 --> 00:00:02,000\nNothing to see here.\n\n")
    ctx = rendered(sample_mkv)
    assert [r for r in render_stage.load(ctx.ws).redacted if r.sidecar_source] == []
    assert list(ctx.ws.redacted_dir.glob("side_*")) == []


def test_an_unparseable_sidecar_falls_back_to_the_embedded_stream(rendered, sample_mkv):
    """§6 step 3 prefers a sidecar, and nothing used to look past a broken one:
    one corrupt `.srt` failed the whole job on a file with good embedded subs."""
    from vidcleaner.pipeline import subtitles as subs_stage

    sidecar = sample_mkv.with_suffix(".srt")
    sidecar.write_bytes(b"\x00\x01 not a subtitle file at all")

    ctx = rendered(sample_mkv)
    subs = subs_stage.load(ctx.ws)
    assert subs.source.kind == "embedded"
    assert subs.cues, "the embedded English stream should have been used instead"

    result = render_stage.load(ctx.ws)
    assert result is not None and Path(result.out_path).is_file()
    assert [r for r in result.redacted if r.sidecar_source] == []


# ------------------------------------------------------------- mp4 -> mkv


def test_mp4_input_is_remuxed_to_matroska(rendered, sample_mp4):
    """C5: mov_text cannot be muxed into Matroska, so it must become srt."""
    ctx = rendered(sample_mp4)
    result = render_stage.load(ctx.ws)
    data = FFmpegRunner().probe(Path(result.out_path))

    assert data["format"]["format_name"].startswith("matroska")
    subs = [s for s in data["streams"] if s["codec_type"] == "subtitle"]
    assert [s["codec_name"] for s in subs] == ["subrip"]


def test_mp4_source_is_left_in_place(rendered, sample_mp4):
    before = sample_mp4.stat()
    rendered(sample_mp4)
    after = sample_mp4.stat()
    assert (before.st_size, before.st_ino) == (after.st_size, after.st_ino)


def test_mp4_render_verifies(rendered, sample_mp4):
    ctx = rendered(sample_mp4)
    run_stage(ctx, "verify")
    result = verify_stage.load(ctx.ws)
    assert result is not None
    assert result.ok, [c.name for c in result.failures]


# -------------------------------------------------------------- other codecs


def test_eight_channel_source_renders_as_flac(rendered, fixture_media):
    ctx = rendered(fixture_media.surround71_mkv)
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    clean = [s for s in data["streams"] if s["codec_type"] == "audio"][0]
    assert clean["codec_name"] == "flac"
    assert int(clean["channels"]) == 8


def test_a_source_without_a_language_tag_gets_none(rendered, fixture_media):
    ctx = rendered(fixture_media.nolang_mkv)
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    clean = [s for s in data["streams"] if s["codec_type"] == "audio"][0]
    assert "language" not in clean.get("tags", {})


def test_lossless_setting_forces_flac(rendered):
    ctx = rendered(clean_track_lossless=True)
    data = FFmpegRunner().probe(Path(render_stage.load(ctx.ws).out_path))
    clean = [s for s in data["streams"] if s["codec_type"] == "audio"][0]
    assert clean["codec_name"] == "flac"


def test_fade_edges_still_leave_the_rest_audible(rendered, runner):
    """C3 through the real pipeline rather than a hand-written graph."""
    ctx = rendered(fade_edges_ms=10)
    out = Path(render_stage.load(ctx.ws).out_path)
    inside = runner.measure_volume(out, window=TimeRange(start=2.2, end=2.8))
    after = runner.measure_volume(out, window=TimeRange(start=8.5, end=9.5))
    assert inside is not None and after is not None
    assert inside.max_db <= INAUDIBLE_DB
    assert after.max_db > -20.0
