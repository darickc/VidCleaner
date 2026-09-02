"""The stage driver: skip/resume, ordering, failure, and artifact collection."""

from __future__ import annotations

import sys
import types

import pytest

from vidcleaner import __version__
from vidcleaner.pipeline.artifacts import CodecPlan, JobSpec, ProbeResult, ProfileSnapshot
from vidcleaner.pipeline.stages import (
    DRY_RUN_STAGES,
    M1_STAGES,
    StageError,
    build_context,
    build_spec,
    deterministic_job_id,
    get_stage,
    run_pipeline,
    run_stage,
)
from vidcleaner.settings_store import AppSettings


@pytest.fixture
def fake_stages(monkeypatch):
    """Register importable stand-ins and return the call log."""
    calls: list[str] = []
    registry: dict[str, str] = {}

    def add(stage: str, *, fail: bool = False, result=None):
        name = f"tests_fake_stage_{stage}"
        module = types.ModuleType(name)
        module.NAME = stage

        def run(ctx, _stage=stage, _fail=fail):
            calls.append(_stage)
            if _fail:
                raise RuntimeError(f"{_stage} exploded")

        module.run = run
        module.load = lambda ws, _r=result: _r
        monkeypatch.setitem(sys.modules, name, module)
        registry[stage] = name

    for stage in M1_STAGES:
        add(stage)
    add.calls = calls  # type: ignore[attr-defined]
    add.registry = registry  # type: ignore[attr-defined]
    return add


@pytest.fixture
def ctx(settings, fake_stages):
    spec = build_spec(
        settings.work_dir / "source.mkv",
        profile=ProfileSnapshot(profile_hash="v1:test"),
        settings=AppSettings(),
    )
    context = build_context(spec, deploy=settings, runner=object())
    context.stage_registry = fake_stages.registry
    return context


# ------------------------------------------------------------------ constants


def test_m1_stages_stop_before_swap():
    assert M1_STAGES[-1] == "verify"
    assert "swap" not in M1_STAGES and "snippets" not in M1_STAGES


def test_dry_run_stops_after_detect():
    assert DRY_RUN_STAGES[-1] == "detect"
    assert "render" not in DRY_RUN_STAGES


# ------------------------------------------------------------------ job specs


def test_deterministic_job_id_is_stable(tmp_path):
    path = tmp_path / "a.mkv"
    assert deterministic_job_id(path, "v1:x") == deterministic_job_id(path, "v1:x")


def test_deterministic_job_id_changes_with_the_profile(tmp_path):
    path = tmp_path / "a.mkv"
    assert deterministic_job_id(path, "v1:x") != deterministic_job_id(path, "v1:y")


def test_deterministic_job_id_changes_with_the_source(tmp_path):
    assert deterministic_job_id(tmp_path / "a.mkv", "v1:x") != deterministic_job_id(
        tmp_path / "b.mkv", "v1:x"
    )


def test_build_spec_strips_secrets_from_the_snapshot(tmp_path):
    from vidcleaner.settings_store import SECRET_FIELDS

    spec = build_spec(
        tmp_path / "a.mkv",
        profile=ProfileSnapshot(profile_hash="v1:x"),
        settings=AppSettings(sonarr_api_key="hunter2", webhook_token="tok"),
    )
    assert not SECRET_FIELDS & set(spec.settings)
    assert "hunter2" not in spec.model_dump_json()


def test_build_spec_records_the_version_and_source(tmp_path):
    spec = build_spec(
        tmp_path / "a.mkv", profile=ProfileSnapshot(profile_hash="v1:x"), settings=AppSettings()
    )
    assert spec.version == __version__
    assert spec.source_path.endswith("a.mkv")
    assert spec.created_at is not None


def test_build_context_writes_job_json(ctx):
    assert ctx.ws.job_spec.is_file()
    assert JobSpec.read(ctx.ws.job_spec).job_id == ctx.spec.job_id


def test_build_context_rehydrates_settings_from_the_spec(ctx):
    assert isinstance(ctx.settings, AppSettings)
    assert ctx.settings.pad_pre_ms == 80


# ------------------------------------------------------------------- registry


def test_get_stage_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="unknown stage"):
        get_stage("nonsense")


def test_get_stage_returns_the_registered_module(fake_stages):
    assert get_stage("probe", fake_stages.registry).NAME == "probe"


# ------------------------------------------------------------- run_stage


def test_run_stage_marks_done(ctx, fake_stages):
    outcome = run_stage(ctx, "probe")
    assert outcome.skipped is False
    assert ctx.ws.is_done("probe")
    assert fake_stages.calls == ["probe"]


def test_run_stage_skips_when_the_marker_is_current(ctx, fake_stages):
    run_stage(ctx, "probe")
    fake_stages.calls.clear()

    outcome = run_stage(ctx, "probe")
    assert outcome.skipped is True
    assert fake_stages.calls == []


def test_force_reruns_a_completed_stage(ctx, fake_stages):
    run_stage(ctx, "probe")
    fake_stages.calls.clear()

    outcome = run_stage(ctx, "probe", force=True)
    assert outcome.skipped is False
    assert fake_stages.calls == ["probe"]


def test_spec_force_reruns_every_stage(ctx, fake_stages):
    run_stage(ctx, "probe")
    fake_stages.calls.clear()
    ctx.spec.force = True

    assert run_stage(ctx, "probe").skipped is False


def test_rerunning_a_stage_invalidates_later_markers(ctx):
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    run_stage(ctx, "probe", force=True)
    assert ctx.ws.completed_stages() == ("probe",)


def test_stage_failure_raises_and_leaves_no_marker(settings, fake_stages):
    fake_stages("detect", fail=True)
    spec = build_spec(
        settings.work_dir / "s.mkv",
        profile=ProfileSnapshot(profile_hash="v1:x"),
        settings=AppSettings(),
    )
    ctx = build_context(spec, deploy=settings, runner=object())
    ctx.stage_registry = fake_stages.registry

    with pytest.raises(StageError, match="detect: detect exploded"):
        run_stage(ctx, "detect")
    assert ctx.ws.is_done("detect") is False


def test_stage_error_carries_the_cause(settings, fake_stages):
    fake_stages("render", fail=True)
    spec = build_spec(
        settings.work_dir / "s.mkv",
        profile=ProfileSnapshot(profile_hash="v1:x"),
        settings=AppSettings(),
    )
    ctx = build_context(spec, deploy=settings, runner=object())
    ctx.stage_registry = fake_stages.registry

    with pytest.raises(StageError) as exc:
        run_stage(ctx, "render")
    assert exc.value.stage == "render"
    assert isinstance(exc.value.cause, RuntimeError)


def test_progress_callback_fires(ctx):
    seen: list[tuple[str, float]] = []
    ctx.on_progress = lambda stage, frac: seen.append((stage, frac))
    run_stage(ctx, "probe")
    assert ("probe", 1.0) in seen


def test_a_failing_progress_callback_does_not_fail_the_stage(ctx):
    def boom(stage, fraction):
        raise RuntimeError("reporter down")

    ctx.on_progress = boom
    assert run_stage(ctx, "probe").skipped is False


# ---------------------------------------------------------------- run_pipeline


def test_run_pipeline_runs_every_m1_stage_in_order(ctx, fake_stages):
    run_pipeline(ctx)
    assert fake_stages.calls == list(M1_STAGES)


def test_dry_run_pipeline_stops_after_detect(settings, fake_stages):
    spec = build_spec(
        settings.work_dir / "s.mkv",
        profile=ProfileSnapshot(profile_hash="v1:x"),
        settings=AppSettings(),
        dry_run=True,
    )
    ctx = build_context(spec, deploy=settings, runner=object())
    ctx.stage_registry = fake_stages.registry

    run_pipeline(ctx)
    assert fake_stages.calls == list(DRY_RUN_STAGES)


def test_run_pipeline_collects_artifacts(settings, fake_stages):
    result_probe = ProbeResult(
        path="/x.mkv", size=1, mtime=1.0, clean_codec=CodecPlan(encoder="aac", reason="r")
    )
    fake_stages("probe", result=result_probe)
    spec = build_spec(
        settings.work_dir / "s.mkv",
        profile=ProfileSnapshot(profile_hash="v1:x"),
        settings=AppSettings(),
    )
    ctx = build_context(spec, deploy=settings, runner=object())
    ctx.stage_registry = fake_stages.registry

    result = run_pipeline(ctx, ["probe"])
    assert result.probe is result_probe
    assert result.timings.keys() == {"probe"}


def test_run_pipeline_resumes_from_markers(ctx, fake_stages):
    run_pipeline(ctx)
    fake_stages.calls.clear()

    run_pipeline(ctx)
    assert fake_stages.calls == [], "a second run should skip every stage"


def test_run_pipeline_rejects_unknown_stages(ctx):
    with pytest.raises(ValueError, match="unknown stages"):
        run_pipeline(ctx, ["probe", "nonsense"])
