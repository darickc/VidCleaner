"""Recording a CLI run in the database (the M1 persistence layer)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from vidcleaner.db.models import Detection as DetectionRow
from vidcleaner.db.models import Job, MediaItem, Title
from vidcleaner.db.session import session_scope
from vidcleaner.pipeline.artifacts import (
    AudioStreamInfo,
    CodecPlan,
    Detection,
    DetectionResult,
    JobSpec,
    ProbeResult,
    ProfileSnapshot,
    TimeRange,
)
from vidcleaner.pipeline.persist import (
    LOCAL_TITLE_ARR_ID,
    ensure_local_title,
    ensure_media_item,
    persist_run,
)


@pytest.fixture
def db(migrated):
    with session_scope() as session:
        yield session


def probe(path: str = "/media/movie.mkv", **kw) -> ProbeResult:
    defaults = dict(
        path=path,
        size=5_000,
        mtime=1.0,
        duration=120.0,
        fingerprint="fp-abc",
        audio=[AudioStreamInfo(index=1, typed_index=0, codec_name="ac3", channels=6)],
        clean_codec=CodecPlan(encoder="ac3", bit_rate=640_000, reason="ac3_passthrough"),
    )
    return ProbeResult(**{**defaults, **kw})


def spec(**kw) -> JobSpec:
    defaults = dict(
        job_id="cli-abc123",
        version="0.1.0",
        source_path="/media/movie.mkv",
        profile=ProfileSnapshot(profile_hash="v1:hash"),
    )
    return JobSpec(**{**defaults, **kw})


def detections(count: int = 2) -> DetectionResult:
    rows = [
        Detection(
            word_raw="Fuck",
            word_canonical="fuck",
            category="strong",
            start_s=float(i),
            end_s=float(i) + 0.4,
            mute_start_s=float(i) - 0.08,
            mute_end_s=float(i) + 0.52,
            source="both",
            confidence=0.95,
            suspicious=i == 0,
            suspicious_reason="testing" if i == 0 else None,
        )
        for i in range(count)
    ]
    return DetectionResult(
        profile_hash="v1:hash",
        detections=rows,
        mute_ranges=[TimeRange(start=r.mute_start_s, end=r.mute_end_s) for r in rows],
    )


# ------------------------------------------------------------ the sentinel


def test_the_local_title_is_created_once(db):
    first = ensure_local_title(db)
    second = ensure_local_title(db)
    assert first.id == second.id
    assert db.scalars(select(Title)).all() == [first]


def test_the_local_title_is_not_enabled(db):
    """Otherwise M3's backfill would treat CLI runs as a real Sonarr series."""
    title = ensure_local_title(db)
    assert title.enabled is False
    assert title.arr_id == LOCAL_TITLE_ARR_ID
    assert title.arr_id < 0, "a negative id can never collide with a real one"


# --------------------------------------------------------------- media items


def test_a_media_item_is_created_and_reused(db):
    first = ensure_media_item(db, probe())
    second = ensure_media_item(db, probe())
    assert first.id == second.id
    assert db.scalars(select(MediaItem)).all() == [first]


def test_media_items_are_keyed_on_path(db):
    a = ensure_media_item(db, probe("/media/a.mkv"))
    b = ensure_media_item(db, probe("/media/b.mkv"))
    assert a.id != b.id


def test_many_local_items_coexist_despite_the_unique_constraint(db):
    """season/episode stay NULL, and SQLite treats NULLs as distinct."""
    for i in range(5):
        ensure_media_item(db, probe(f"/media/{i}.mkv"))
    items = db.scalars(select(MediaItem)).all()
    assert len(items) == 5
    assert all(i.season is None and i.episode is None for i in items)


def test_media_item_metadata_is_refreshed(db):
    ensure_media_item(db, probe())
    item = ensure_media_item(db, probe(size=9_999, duration=300.0, fingerprint="fp-new"))
    assert item.size == 9_999
    assert item.duration == 300.0
    assert item.source_fingerprint == "fp-new"


# ------------------------------------------------------------------- runs


def test_a_successful_run_is_recorded(db):
    result = persist_run(db, spec(), probe=probe(), detections=detections(), state="done")
    job = db.get(Job, result.job_id)
    assert job is not None
    assert job.state == "done"
    assert job.trigger == "manual"
    assert job.dry_run is False
    assert job.source_fingerprint == "fp-abc"
    assert result.detections == 2


def test_detections_are_written_with_both_foreign_keys(db):
    result = persist_run(db, spec(), probe=probe(), detections=detections(3), state="done")
    rows = db.scalars(select(DetectionRow)).all()
    assert len(rows) == 3
    assert all(r.job_id == result.job_id for r in rows)
    assert all(r.media_item_id == result.media_item_id for r in rows)


def test_detection_fields_survive_the_round_trip(db):
    persist_run(db, spec(), probe=probe(), detections=detections(1), state="done")
    row = db.scalars(select(DetectionRow)).one()
    assert row.word_canonical == "fuck"
    assert row.category == "strong"
    assert row.source == "both"
    assert row.confidence == 0.95
    assert row.suspicious is True
    assert row.muted is True


def test_the_json_only_field_is_not_persisted(db):
    """`suspicious_reason` has no column; excluding it keeps persist mechanical."""
    persist_run(db, spec(), probe=probe(), detections=detections(1), state="done")
    row = db.scalars(select(DetectionRow)).one()
    assert not hasattr(row, "suspicious_reason")


def test_the_media_item_is_marked_clean(db):
    result = persist_run(db, spec(), probe=probe(), detections=detections(), state="done")
    item = db.get(MediaItem, result.media_item_id)
    assert item.status == "clean"
    assert item.last_job_id == result.job_id
    assert item.cleaned_at is not None


def test_a_dry_run_leaves_the_item_pending(db):
    result = persist_run(
        db, spec(dry_run=True), probe=probe(), detections=detections(), state="done"
    )
    item = db.get(MediaItem, result.media_item_id)
    assert item.status == "pending"
    assert item.cleaned_at is None
    assert db.get(Job, result.job_id).dry_run is True


def test_a_failure_is_recorded_with_its_stage(db):
    result = persist_run(
        db,
        spec(),
        probe=probe(),
        detections=None,
        state="failed",
        stage="render",
        error="ffmpeg blew up",
    )
    job = db.get(Job, result.job_id)
    assert (job.state, job.stage, job.error) == ("failed", "render", "ffmpeg blew up")
    assert db.get(MediaItem, result.media_item_id).status == "failed"


def test_already_clean_is_recorded(db):
    result = persist_run(db, spec(), probe=probe(), detections=None, state="already_clean")
    assert db.get(Job, result.job_id).state == "already_clean"
    assert db.get(MediaItem, result.media_item_id).status == "already_clean"


def test_rerunning_the_same_job_id_replaces_its_detections(db):
    """The deterministic CLI job id means re-runs must not accumulate rows."""
    persist_run(db, spec(), probe=probe(), detections=detections(3), state="done")
    persist_run(db, spec(), probe=probe(), detections=detections(1), state="done")
    assert len(db.scalars(select(DetectionRow)).all()) == 1
    assert db.get(Job, "cli-abc123").attempts == 2


def test_the_profile_snapshot_is_stored(db):
    result = persist_run(db, spec(), probe=probe(), detections=detections(), state="done")
    payload = json.loads(db.get(Job, result.job_id).profile_snapshot_json)
    assert payload["profile_hash"] == "v1:hash"


def test_timings_default_to_an_empty_object(db):
    result = persist_run(db, spec(), probe=probe(), detections=None, state="done")
    assert json.loads(db.get(Job, result.job_id).timings_json) == {}


def test_the_model_and_subtitle_source_are_recorded(db):
    result = persist_run(
        db,
        spec(),
        probe=probe(),
        detections=None,
        state="done",
        model_used="large-v3-turbo",
        subtitle_source="embedded_preferred_language",
    )
    job = db.get(Job, result.job_id)
    assert job.model_used == "large-v3-turbo"
    assert job.subtitle_source == "embedded_preferred_language"


def test_two_files_get_two_jobs_and_two_items(db):
    a = persist_run(db, spec(), probe=probe("/media/a.mkv"), detections=None, state="done")
    b = persist_run(
        db, spec(job_id="cli-def456"), probe=probe("/media/b.mkv"), detections=None, state="done"
    )
    assert a.job_id != b.job_id
    assert a.media_item_id != b.media_item_id
    assert len(db.scalars(select(Title)).all()) == 1, "one sentinel title serves both"


def test_the_rollup_query_from_plan_section_five_works(db):
    """§5's per-item word counts, straight from the persisted rows.

    Note the `cast`: §5 writes `SUM(muted)`, but `muted` is a Boolean column, so
    SQLAlchemy applies the Boolean result processor and hands back `True`
    instead of a count. M4's rollup must cast to Integer.
    """
    from sqlalchemy import Integer, cast, func

    result = persist_run(db, spec(), probe=probe(), detections=detections(3), state="done")
    rows = db.execute(
        select(
            DetectionRow.word_canonical,
            DetectionRow.category,
            func.count(),
            func.sum(cast(DetectionRow.muted, Integer)),
        )
        .where(
            DetectionRow.media_item_id == result.media_item_id,
            DetectionRow.job_id == result.job_id,
            DetectionRow.whitelisted.is_(False),
        )
        .group_by(DetectionRow.word_canonical, DetectionRow.category)
    ).all()
    assert rows == [("fuck", "strong", 3, 3)]
