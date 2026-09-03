"""The review clips, cut for real -- §6 step 10 and §12's volumedetect assertion."""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.matching.compiler import build_matcher
from vidcleaner.pipeline import snippets as snippets_stage
from vidcleaner.pipeline.artifacts import (
    Detection,
    DetectionResult,
    ProfileSnapshot,
    TimeRange,
)
from vidcleaner.pipeline.ffmpeg import INAUDIBLE_DB, FFmpegRunner
from vidcleaner.pipeline.stages import build_context, build_spec, run_stage
from vidcleaner.settings_store import AppSettings

MUTE = TimeRange(start=2.0, end=3.0)


def detections_for(*ranges: TimeRange) -> DetectionResult:
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
            )
            for r in ranges
        ],
        mute_ranges=list(ranges),
    )


@pytest.fixture
def clipped(settings, sample_mkv):
    """probe -> extract -> (injected detections) -> snippets, on real media."""

    def go(*ranges: TimeRange):
        spec = build_spec(
            sample_mkv,
            profile=ProfileSnapshot(profile_hash="v1:test"),
            settings=AppSettings(drift_check=False),
        )
        ctx = build_context(spec, deploy=settings)
        ctx.matcher = build_matcher()
        for stage in ("probe", "extract"):
            run_stage(ctx, stage)
        detections_for(*(ranges or (MUTE,))).write(ctx.ws.detections_json)
        ctx.ws.mark_done("detect")
        run_stage(ctx, "snippets")
        return ctx

    return go


def test_every_detection_gets_all_three_files(clipped) -> None:
    ctx = clipped(MUTE, TimeRange(start=7.0, end=7.6))
    result = snippets_stage.load(ctx.ws)

    assert [s.detection_index for s in result.snippets] == [0, 1]
    assert not result.warnings and not result.skipped
    for snippet in result.snippets:
        directory = Path(result.root) / snippet.rel_dir
        assert sorted(p.name for p in directory.iterdir()) == [
            "clean.m4a",
            "orig.m4a",
            "wave.png",
        ]
        assert all(p.stat().st_size > 0 for p in directory.iterdir())


def test_the_clean_clip_is_silent_where_the_original_is_not(clipped) -> None:
    """The tripwire: a sign error on the offset would move the silence."""
    ctx = clipped()
    snippet = snippets_stage.load(ctx.ws).snippets[0]
    directory = Path(snippets_stage.load(ctx.ws).root) / snippet.rel_dir
    runner = FFmpegRunner()

    # Where the mute lands inside a clip centred on it.
    inside = TimeRange(start=2.1, end=2.9)
    original = runner.measure_volume(directory / "orig.m4a", window=inside)
    cleaned = runner.measure_volume(directory / "clean.m4a", window=inside)

    assert original is not None and original.mean_db > INAUDIBLE_DB + 20
    assert cleaned is not None and cleaned.inaudible


def test_audio_outside_the_mute_survives_in_the_clean_clip(clipped) -> None:
    ctx = clipped()
    result = snippets_stage.load(ctx.ws)
    directory = Path(result.root) / result.snippets[0].rel_dir
    control = TimeRange(start=0.2, end=1.2)

    cleaned = FFmpegRunner().measure_volume(directory / "clean.m4a", window=control)
    assert cleaned is not None and not cleaned.inaudible


def test_the_clips_land_under_the_config_snippet_root(clipped, settings) -> None:
    """Not /work: the Item page has to outlive the work-dir collector."""
    ctx = clipped()
    root = Path(snippets_stage.load(ctx.ws).root)
    assert root.is_relative_to(settings.snippets_dir)
    assert root.name == ctx.spec.job_id


def test_a_run_with_no_detections_is_recorded_not_failed(settings, sample_mkv) -> None:
    spec = build_spec(
        sample_mkv,
        profile=ProfileSnapshot(profile_hash="v1:test"),
        settings=AppSettings(drift_check=False),
    )
    ctx = build_context(spec, deploy=settings)
    DetectionResult(profile_hash="v1:test").write(ctx.ws.detections_json)
    ctx.ws.mark_done("detect")

    run_stage(ctx, "snippets")
    result = snippets_stage.load(ctx.ws)
    assert result.snippets == []
    assert result.skipped == ["no_detections"]


def test_a_pruned_work_dir_skips_rather_than_fails(settings, sample_mkv) -> None:
    """Resume after `prune_work_dir`: audio.wav is gone and the swap already
    committed, so there is nothing here worth failing a job over."""
    spec = build_spec(
        sample_mkv,
        profile=ProfileSnapshot(profile_hash="v1:test"),
        settings=AppSettings(drift_check=False),
    )
    ctx = build_context(spec, deploy=settings)
    detections_for(MUTE).write(ctx.ws.detections_json)
    ctx.ws.mark_done("detect")

    run_stage(ctx, "snippets")
    assert snippets_stage.load(ctx.ws).skipped == ["no_audio"]
