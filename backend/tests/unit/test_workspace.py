"""The per-job work dir, stage markers and resume semantics."""

from __future__ import annotations

import pytest

from vidcleaner import __version__
from vidcleaner.db.constants import JOB_STAGES
from vidcleaner.pipeline.workspace import Workspace, atomic_write_text


@pytest.fixture
def ws(tmp_path):
    return Workspace("job-1", tmp_path / "job-1").ensure()


def test_for_job_uses_the_configured_work_dir(settings):
    w = Workspace.for_job("abc", settings)
    assert w.root == settings.work_dir / "abc"
    assert w.job_id == "abc"


def test_ensure_creates_the_subdirectories(ws):
    for path in (ws.root, ws.subs_dir, ws.redacted_dir):
        assert path.is_dir()


def test_ensure_is_idempotent(ws):
    assert ws.ensure() is ws


def test_artifact_paths_live_under_the_root(ws):
    for path in (
        ws.job_spec,
        ws.probe_json,
        ws.audio_wav,
        ws.subs_json,
        ws.transcript_json,
        ws.detections_json,
        ws.graph_txt,
        ws.render_json,
        ws.verify_json,
        ws.out_mkv,
        ws.ffmpeg_log,
    ):
        assert path.parent == ws.root


def test_artifact_paths_are_distinct(ws):
    paths = [
        ws.job_spec,
        ws.probe_json,
        ws.audio_wav,
        ws.subs_json,
        ws.transcript_json,
        ws.detections_json,
        ws.graph_txt,
        ws.render_json,
        ws.verify_json,
        ws.out_mkv,
        ws.ffmpeg_log,
    ]
    assert len(set(paths)) == len(paths)


# ---------------------------------------------------------------------- markers


def test_marker_rejects_an_unknown_stage(ws):
    with pytest.raises(ValueError, match="unknown stage"):
        ws.marker("nonsense")


@pytest.mark.parametrize("stage", JOB_STAGES)
def test_every_job_stage_has_a_marker_path(ws, stage):
    assert ws.marker(stage).name == f"{stage}.done"


def test_mark_done_then_is_done(ws):
    assert ws.is_done("probe") is False
    ws.mark_done("probe", elapsed_s=1.25, detail={"streams": 3})
    assert ws.is_done("probe") is True

    marker = ws.read_marker("probe")
    assert marker is not None
    assert marker.stage == "probe"
    assert marker.version == __version__
    assert marker.elapsed_s == 1.25
    assert marker.detail == {"streams": 3}


def test_a_marker_from_a_different_version_is_not_done(ws):
    """A code upgrade must never resume onto artifacts written by other code."""
    ws.mark_done("probe")
    marker = ws.read_marker("probe")
    assert marker is not None
    atomic_write_text(
        ws.marker("probe"), marker.model_copy(update={"version": "0.0.1"}).model_dump_json()
    )
    assert ws.is_done("probe") is False


def test_a_corrupt_marker_is_not_done(ws):
    ws.marker("probe").write_text("{not json", encoding="utf-8")
    assert ws.is_done("probe") is False
    assert ws.read_marker("probe") is None


def test_read_marker_is_none_when_absent(ws):
    assert ws.read_marker("render") is None


def test_completed_stages_follows_job_stages_order(ws):
    ws.mark_done("detect")
    ws.mark_done("probe")
    ws.mark_done("extract")
    assert ws.completed_stages() == ("probe", "extract", "detect")


def test_clear_from_drops_that_stage_and_every_later_one(ws):
    for stage in ("probe", "extract", "subtitles", "transcribe", "detect", "render"):
        ws.mark_done(stage)
    ws.clear_from("subtitles")
    assert ws.completed_stages() == ("probe", "extract")


def test_clear_from_is_safe_when_nothing_is_marked(ws):
    ws.clear_from("probe")
    assert ws.completed_stages() == ()


def test_clear_from_rejects_an_unknown_stage(ws):
    with pytest.raises(ValueError, match="unknown stage"):
        ws.clear_from("nonsense")


def test_clear_all(ws):
    for stage in JOB_STAGES:
        ws.mark_done(stage)
    ws.clear_all()
    assert ws.completed_stages() == ()


# --------------------------------------------------------------- atomic writes


def test_atomic_write_leaves_no_temp_file(ws):
    atomic_write_text(ws.probe_json, '{"a": 1}')
    assert ws.probe_json.read_text() == '{"a": 1}'
    assert not list(ws.root.glob(".*.tmp"))


def test_atomic_write_creates_parent_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "file.json"
    atomic_write_text(target, "{}")
    assert target.read_text() == "{}"


def test_atomic_write_overwrites(ws):
    atomic_write_text(ws.probe_json, "first")
    atomic_write_text(ws.probe_json, "second")
    assert ws.probe_json.read_text() == "second"


# ------------------------------------------------------------------------ disk


def test_free_bytes_is_positive(ws):
    assert ws.free_bytes() > 0


def test_free_bytes_works_before_the_root_exists(tmp_path):
    assert Workspace("nope", tmp_path / "nope").free_bytes() > 0


def test_size_bytes_counts_artifacts(ws):
    atomic_write_text(ws.probe_json, "x" * 100)
    atomic_write_text(ws.subs_json, "y" * 50)
    assert ws.size_bytes() == 150


def test_equality_and_hashing(tmp_path):
    a = Workspace("j", tmp_path / "j")
    b = Workspace("j", tmp_path / "j")
    assert a == b and hash(a) == hash(b)
    assert a != Workspace("k", tmp_path / "k")
    assert a != "not a workspace"
