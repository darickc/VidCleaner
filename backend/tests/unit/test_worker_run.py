"""The worker run loop, driven through a fake stage registry.

No ffmpeg and no torch: `tests/support/stages/` provides a complete registry that
writes the real artifact models, so everything the loop actually does -- state
transitions, progress, the job timeline, retry classification, resume, persistence,
work-dir pruning -- is exercised in milliseconds.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.support.stages import CONTROL, REGISTRY
from vidcleaner.config import Settings
from vidcleaner.db.models import Backup, Detection, Job, JobLog, MediaItem, Title
from vidcleaner.db.session import session_scope, utcnow
from vidcleaner.matching.profile import ensure_seed_data
from vidcleaner.pipeline.stages import StageError
from vidcleaner.pipeline.workspace import Workspace
from vidcleaner.worker.claim import MAX_ATTEMPTS, enqueue
from vidcleaner.worker.runner import Worker


@pytest.fixture(autouse=True)
def control():
    CONTROL.reset()
    yield CONTROL
    CONTROL.reset()


@pytest.fixture
def worker(migrated: Settings, monkeypatch):
    """A Worker whose jobs run the fake stages."""
    from vidcleaner.pipeline import stages as stages_mod

    monkeypatch.setattr(stages_mod, "_STAGE_MODULES", dict(REGISTRY))
    return Worker(migrated, poll_interval=0.01)


@pytest.fixture
def library(migrated: Settings, tmp_path: Path):
    """A tracked episode whose file really exists, inside a `/media` tree."""
    ensure_seed_data()
    folder = migrated.media_dir / "tv" / "Show"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "S01E01.mkv"
    source.write_bytes(b"o" * CONTROL.source_size)
    with session_scope() as session:
        title = Title(kind="series", arr_id=42, title="Show", enabled=True)
        session.add(title)
        session.flush()
        item = MediaItem(
            title_id=title.id,
            kind="episode",
            season=1,
            episode=1,
            path=str(source),
            status="pending",
        )
        session.add(item)
        session.flush()
        return item.id, source


def queue_job(item_id: int, **kw) -> str:
    with session_scope() as session:
        return enqueue(session, media_item_id=item_id, trigger="manual", **kw).job_id


def job_row(job_id: str) -> Job:
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def timeline(job_id: str) -> list[str]:
    with session_scope() as session:
        return [
            r.msg
            for r in session.scalars(
                select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.id)
            )
        ]


# ------------------------------------------------------------------ happy path


def test_a_queued_job_runs_every_stage_and_lands_done(worker, library) -> None:
    item_id, source = library
    job_id = queue_job(item_id)

    assert worker.poll_once() is True
    assert CONTROL.ran == [
        "probe",
        "extract",
        "subtitles",
        "transcribe",
        "detect",
        "render",
        "verify",
        "swap",
        "refresh",
        "snippets",
    ]
    job = job_row(job_id)
    assert job.state == "done"
    assert job.progress_pct == 100.0
    assert job.claimed_by is None and job.finished_at is not None
    assert job.attempts == 1, "claims, not failures"


def test_the_library_file_is_swapped_and_recorded(worker, library) -> None:
    item_id, source = library
    original = source.read_bytes()
    job_id = queue_job(item_id)
    worker.poll_once()

    assert source.read_bytes() == b"c" * CONTROL.out_size
    backup = worker.settings.backups_dir / "tv" / "Show" / "S01E01.mkv"
    assert backup.read_bytes() == original

    with session_scope() as session:
        assert session.scalars(select(Backup)).one().state == "kept"
        item = session.get(MediaItem, item_id)
        assert item.status == "clean" and item.last_job_id == job_id
        assert len(session.scalars(select(Detection)).all()) == CONTROL.detections


def test_the_timings_come_from_the_markers(worker, library) -> None:
    import json

    item_id, _ = library
    job_id = queue_job(item_id)
    worker.poll_once()
    timings = json.loads(job_row(job_id).timings_json)
    assert set(timings) == set(CONTROL.ran)


def test_the_timeline_records_the_whole_run(worker, library) -> None:
    item_id, _ = library
    job_id = queue_job(item_id)
    worker.poll_once()
    messages = timeline(job_id)
    assert any(m.startswith("claimed") for m in messages)
    assert any("probe done" in m for m in messages)
    assert any("swapped" in m for m in messages)
    assert any("detected" in m for m in messages)


def test_the_big_artifacts_are_pruned_but_the_evidence_stays(worker, library) -> None:
    """Nothing in PLAN.md reclaims `/work`, and `out.mkv` is source-sized."""
    item_id, _ = library
    job_id = queue_job(item_id)
    worker.poll_once()

    ws = Workspace.for_job(job_id, worker.settings)
    assert not ws.out_mkv.exists() and not ws.audio_wav.exists()
    assert not ws.graph_txt.exists()
    assert ws.detections_json.is_file(), "M4 reads this back"
    assert ws.job_spec.is_file() and ws.probe_json.is_file()
    assert (worker.settings.snippets_dir / job_id).is_dir(), "review clips are not in /work"


def test_an_empty_queue_is_no_work(worker) -> None:
    assert worker.poll_once() is False


# --------------------------------------------------------------------- dry run


def test_a_dry_run_stops_after_detecting(worker, library) -> None:
    item_id, source = library
    original = source.read_bytes()
    job_id = queue_job(item_id, dry_run=True)
    worker.poll_once()

    assert CONTROL.ran == ["probe", "extract", "subtitles", "transcribe", "detect"]
    assert job_row(job_id).state == "done"
    assert source.read_bytes() == original, "the library is untouched"
    with session_scope() as session:
        assert session.get(MediaItem, item_id).status == "pending"


# ---------------------------------------------------------------- already clean


def test_an_already_clean_file_short_circuits_after_probe(worker, library) -> None:
    """§4's idempotency loop. `parse_probe` always computed the flag; before this
    nothing outside the CLI acted on it."""
    item_id, source = library
    original = source.read_bytes()
    CONTROL.already_clean = True
    job_id = queue_job(item_id)
    worker.poll_once()

    assert CONTROL.ran == ["probe"]
    assert job_row(job_id).state == "already_clean"
    assert source.read_bytes() == original
    with session_scope() as session:
        assert session.get(MediaItem, item_id).status == "already_clean"


def test_force_overrides_already_clean(worker, library) -> None:
    item_id, _ = library
    CONTROL.already_clean = True
    job_id = queue_job(item_id, force=True)
    worker.poll_once()
    assert "render" in CONTROL.ran
    assert job_row(job_id).state == "done"


# ------------------------------------------------------------------- failures


def test_a_render_failure_is_terminal(worker, library) -> None:
    """§6: retrying re-encodes twenty minutes to fail identically."""
    item_id, source = library
    original = source.read_bytes()
    CONTROL.fail["render"] = StageError("render", "ffmpeg exploded")
    job_id = queue_job(item_id)
    worker.poll_once()

    job = job_row(job_id)
    assert (job.state, job.stage) == ("failed", "render")
    assert job.retry_at is None
    assert source.read_bytes() == original, "the library is untouched"
    with session_scope() as session:
        assert session.get(MediaItem, item_id).status == "failed"


def test_a_verify_failure_leaves_the_library_alone(worker, library) -> None:
    item_id, source = library
    original = source.read_bytes()
    CONTROL.verify_ok = False
    job_id = queue_job(item_id)
    worker.poll_once()

    assert job_row(job_id).state == "failed"
    assert source.read_bytes() == original
    assert "swap" not in CONTROL.ran


def test_an_early_failure_is_requeued_with_a_backoff(worker, library) -> None:
    item_id, _ = library
    CONTROL.fail["subtitles"] = StageError("subtitles", "transient")
    job_id = queue_job(item_id)
    worker.poll_once()

    job = job_row(job_id)
    assert job.state == "queued"
    assert job.retry_at is not None and job.retry_at > utcnow() + timedelta(seconds=30)
    assert job.error == "transient"
    # Not claimable yet, so the loop does not spin on it.
    assert worker.poll_once() is False


def test_a_job_that_keeps_failing_is_retired(worker, library) -> None:
    item_id, _ = library
    CONTROL.fail["subtitles"] = StageError("subtitles", "always")
    job_id = queue_job(item_id)

    for _ in range(MAX_ATTEMPTS):
        with session_scope() as session:
            session.get(Job, job_id).retry_at = None
        worker.poll_once()

    job = job_row(job_id)
    assert job.state == "failed" and job.attempts == MAX_ATTEMPTS


def test_a_refresh_failure_still_leaves_the_job_done(worker, library) -> None:
    """The swap already committed: the library file is correct and a retry would
    re-run the one non-idempotent stage."""
    item_id, source = library
    CONTROL.fail["refresh"] = StageError("refresh", "sonarr is down")
    job_id = queue_job(item_id)
    worker.poll_once()

    assert job_row(job_id).state == "done"
    assert source.read_bytes() == b"c" * CONTROL.out_size
    assert any("continuing" in m for m in timeline(job_id))
    with session_scope() as session:
        assert session.get(MediaItem, item_id).status == "clean"


def test_a_vanished_source_is_stale_after_one_retry(worker, library) -> None:
    item_id, source = library
    source.unlink()
    job_id = queue_job(item_id)

    worker.poll_once()
    assert job_row(job_id).state == "queued", "requeued once, per §6"
    with session_scope() as session:
        session.get(Job, job_id).retry_at = None
    worker.poll_once()
    assert job_row(job_id).state == "stale"


# --------------------------------------------------------------------- resume


def test_a_resumed_job_skips_completed_stages(worker, library) -> None:
    item_id, _ = library
    CONTROL.fail["render"] = StageError("render", "boom")
    job_id = queue_job(item_id)
    worker.poll_once()
    first = list(CONTROL.ran)
    assert "detect" in first

    CONTROL.reset()
    with session_scope() as session:
        job = session.get(Job, job_id)
        job.state = "queued"
        job.retry_at = None
        job.attempts = 0
    worker.poll_once()

    assert "probe" not in CONTROL.ran, "resumed from the markers"
    assert CONTROL.ran[0] == "render"
    assert job_row(job_id).state == "done"


def test_a_resumed_job_does_not_report_zero_percent(worker, library) -> None:
    """`run_stage` skips a completed stage without calling progress at all."""
    item_id, _ = library
    CONTROL.fail["swap"] = StageError("swap", "boom")
    job_id = queue_job(item_id)
    worker.poll_once()

    CONTROL.reset()
    CONTROL.fail["swap"] = StageError("swap", "boom again")
    with session_scope() as session:
        job = session.get(Job, job_id)
        job.state = "queued"
        job.retry_at = None
    worker.poll_once()
    assert job_row(job_id).progress_pct > 50


def test_a_changed_profile_discards_the_work_dir(worker, library) -> None:
    from vidcleaner.db.models import WhitelistEntry
    from vidcleaner.matching.profile import clear_matcher_cache

    item_id, _ = library
    CONTROL.fail["render"] = StageError("render", "boom")
    job_id = queue_job(item_id)
    worker.poll_once()

    with session_scope() as session:
        session.add(
            WhitelistEntry(scope="item", scope_id=item_id, canonical_word="shit", context_text=None)
        )
    clear_matcher_cache()

    CONTROL.reset()
    with session_scope() as session:
        job = session.get(Job, job_id)
        job.state = "queued"
        job.retry_at = None
        job.attempts = 0
    worker.poll_once()

    assert CONTROL.ran[0] == "probe", "everything re-ran under the new profile"
    assert any("work dir discarded" in m for m in timeline(job_id))


# ------------------------------------------------------------------ the loop


def test_the_loop_recovers_a_stale_job_on_startup(worker, library) -> None:
    item_id, _ = library
    job_id = queue_job(item_id)
    with session_scope() as session:
        job = session.get(Job, job_id)
        job.state = "transcribing"
        job.claimed_by = "a-dead-worker"
        job.heartbeat = None

    import threading

    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if job_row(job_id).state == "done":
                break
            import time

            time.sleep(0.02)
    finally:
        worker.request_stop()
        thread.join(5)
    assert job_row(job_id).state == "done"


def test_the_scheduler_only_runs_when_the_queue_is_idle(worker, library) -> None:
    ran = worker.scheduler.tick()
    assert "recover_stale" in ran and "gc_work_dirs" in ran
    # Nothing is due a second time immediately.
    assert worker.scheduler.tick() == []


# --------------------------------------------- re-cleaning starts from the original


def test_a_forced_re_clean_restores_the_original_first(worker, library) -> None:
    """Found by the M4 demo. Without this the second run reads *our own output*:
    the previous Clean track becomes the new "Original", the file grows a track per
    run, and a whitelisted word can never become audible again."""
    item_id, source = library
    original = source.read_bytes()
    queue_job(item_id)
    worker.poll_once()
    assert source.read_bytes() != original, "the first run swapped in the clean file"

    queue_job(item_id, force=True)
    worker.poll_once()

    # What the second run actually cleaned: the pristine original, put back first.
    spec_path = Path(job_row(_last_job(item_id)).work_dir or "") / "job.json"
    assert original == worker.settings.backups_dir.joinpath("tv/Show/S01E01.mkv").read_bytes()
    assert spec_path.is_file()
    with session_scope() as session:
        states = [b.state for b in session.scalars(select(Backup)).all()]
    assert states.count("kept") == 1, "exactly one original is restorable"


def test_the_restore_is_recorded_on_the_job_timeline(worker, library) -> None:
    item_id, _ = library
    queue_job(item_id)
    worker.poll_once()

    job_id = queue_job(item_id, force=True)
    worker.poll_once()
    assert any("restored the original" in msg for msg in timeline(job_id))


def test_a_dry_run_never_restores(worker, library) -> None:
    """It must not touch the library at all -- §6 stops it after `detecting`."""
    item_id, source = library
    queue_job(item_id)
    worker.poll_once()
    cleaned = source.read_bytes()

    queue_job(item_id, force=True, dry_run=True)
    worker.poll_once()
    assert source.read_bytes() == cleaned


def test_a_first_clean_has_nothing_to_restore(worker, library) -> None:
    item_id, _ = library
    job_id = queue_job(item_id, force=True)
    worker.poll_once()
    assert not any("restored the original" in msg for msg in timeline(job_id))
    assert job_row(job_id).state == "done"


def _last_job(item_id: int) -> str:
    with session_scope() as session:
        return session.get(MediaItem, item_id).last_job_id


# --------------------------------------------------------- §6's audit pass (M5)


def audit_job(item_id: int, *, promotion: bool = False) -> str:
    """Phase 1 is a dry run against the backup; phase 2 forces a re-render."""
    with session_scope() as session:
        return enqueue(
            session,
            media_item_id=item_id,
            trigger="audit",
            stt_mode="audit",
            dry_run=not promotion,
            force=promotion,
        ).job_id


def spec_of(job_id: str) -> dict:
    import json

    return json.loads((Path(job_row(job_id).work_dir or "") / "job.json").read_text())


def detections_of(job_id: str) -> list[str]:
    with session_scope() as session:
        return [
            d.word_canonical
            for d in session.scalars(
                select(Detection).where(Detection.job_id == job_id).order_by(Detection.start_s)
            )
        ]


def test_phase_one_reads_the_backup_not_the_library_file(worker, library) -> None:
    """§6 re-checks the *original* audio, and the library file no longer holds it:
    `probe` picks the default audio stream, which after a clean is the muted Clean
    track. So auditing the library file would transcribe silence where the words are.
    """
    item_id, source = library
    queue_job(item_id)
    worker.poll_once()

    job_id = audit_job(item_id)
    worker.poll_once()

    backup = worker.settings.backups_dir / "tv/Show/S01E01.mkv"
    assert spec_of(job_id)["source_path"] == str(backup)
    assert spec_of(job_id)["source_path"] != str(source)


def test_phase_one_needs_no_force_and_so_keeps_its_stage_markers(worker, library) -> None:
    """The backup carries no `VIDCLEANER_PROFILE_HASH`, so `probe.already_clean` is
    False without forcing -- and `force` disables marker skipping, which would make a
    killed 30-minute transcription restart from nothing on every container bounce."""
    item_id, _ = library
    queue_job(item_id)
    worker.poll_once()

    job_id = audit_job(item_id)
    worker.poll_once()
    assert job_row(job_id).force is False
    assert job_row(job_id).state == "done"


def test_phase_one_stops_after_detect_and_touches_nothing(worker, library) -> None:
    item_id, source = library
    queue_job(item_id)
    worker.poll_once()
    cleaned = source.read_bytes()

    audit_job(item_id)
    worker.poll_once()

    assert CONTROL.ran[-5:] == ["probe", "extract", "subtitles", "transcribe", "detect"]
    assert "render" not in CONTROL.ran[-5:]
    assert "swap" not in CONTROL.ran[-5:]
    assert source.read_bytes() == cleaned, "the library file is byte-identical"


def test_phase_one_leaves_the_item_clean(worker, library) -> None:
    """`_item_status` would map this dry run to `pending`, which drops the item out of
    `sync.CLEAN_STATUSES` and has the hourly sync re-enqueue a clean file forever --
    the loop the M4 log says the audit must not start. `last_job_id` still moves,
    which is what closes it."""
    item_id, _ = library
    queue_job(item_id)
    worker.poll_once()

    job_id = audit_job(item_id)
    worker.poll_once()
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        assert item.status == "clean"
        assert item.last_job_id == job_id


def test_phase_one_records_the_union_not_just_what_it_heard(worker, library) -> None:
    """The heart of it. `detect(mode="audit")` is STT-only, and M2 measured full mode
    missing 22 of windowed's 49 detections -- so recording only what this pass heard
    would drop words the file already has muted."""
    item_id, _ = library
    CONTROL.detections = 3
    clean_job = queue_job(item_id)
    worker.poll_once()
    assert len(detections_of(clean_job)) == 3

    # The audit hears only one of them (a shorter fake transcript).
    CONTROL.detections = 1
    job_id = audit_job(item_id)
    worker.poll_once()

    assert len(detections_of(job_id)) == 3, "the three prior mutes survive the audit"


def test_phase_one_reports_what_it_found_on_the_timeline(worker, library) -> None:
    item_id, _ = library
    queue_job(item_id)
    worker.poll_once()
    job_id = audit_job(item_id)
    worker.poll_once()
    assert any("audited against the backup original" in msg for msg in timeline(job_id))


def test_the_promotion_skips_transcribe_and_detect(worker, library) -> None:
    """It already holds the set phase 1 computed; re-running the pass would cost
    another ~30 minutes to produce the same ranges."""
    item_id, _ = library
    queue_job(item_id)
    worker.poll_once()
    audit_job(item_id)
    worker.poll_once()

    CONTROL.ran.clear()
    job_id = audit_job(item_id, promotion=True)
    worker.poll_once()

    assert "transcribe" not in CONTROL.ran
    assert "detect" not in CONTROL.ran
    assert "extract" in CONTROL.ran, "snippets cuts its review clips from audio.wav"
    assert "render" in CONTROL.ran and "swap" in CONTROL.ran
    assert job_row(job_id).state == "done"


def test_the_promotion_renders_the_merged_set(worker, library) -> None:
    item_id, _ = library
    CONTROL.detections = 3
    queue_job(item_id)
    worker.poll_once()
    CONTROL.detections = 1
    audit_job(item_id)
    worker.poll_once()

    job_id = audit_job(item_id, promotion=True)
    worker.poll_once()
    assert len(detections_of(job_id)) == 3


def test_the_promotion_leaves_exactly_one_kept_backup(worker, library) -> None:
    """It restores first, so backups do not accumulate a chain per audit."""
    item_id, _ = library
    queue_job(item_id)
    worker.poll_once()
    audit_job(item_id)
    worker.poll_once()
    audit_job(item_id, promotion=True)
    worker.poll_once()

    with session_scope() as session:
        states = [b.state for b in session.scalars(select(Backup)).all()]
    assert states.count("kept") == 1


def test_a_failed_restore_is_terminal_for_a_promotion(worker, library, monkeypatch) -> None:
    """Unattended, so nobody would notice a second Clean track being rendered over the
    first -- which is exactly what §6's "never re-encodes the clean track twice"
    forbids. A user-clicked reprocess still warns and continues."""
    item_id, source = library
    queue_job(item_id)
    worker.poll_once()
    cleaned = source.read_bytes()

    from vidcleaner.pipeline import persist as persist_mod

    def boom(*_a, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr(persist_mod, "restore_item", boom)

    job_id = audit_job(item_id, promotion=True)
    worker.poll_once()

    assert job_row(job_id).state == "failed"
    assert source.read_bytes() == cleaned, "the library is untouched"


def test_a_failed_restore_only_warns_for_a_user_reprocess(worker, library, monkeypatch) -> None:
    item_id, _ = library
    queue_job(item_id)
    worker.poll_once()

    from vidcleaner.pipeline import persist as persist_mod

    monkeypatch.setattr(
        persist_mod, "restore_item", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("nope"))
    )
    job_id = queue_job(item_id, force=True)
    worker.poll_once()

    assert job_row(job_id).state == "done"
    assert any("could not restore" in msg for msg in timeline(job_id))


# ------------------------------------------------------------ the disk gate (M5)


def test_a_full_work_volume_pauses_the_queue_instead_of_failing_jobs(
    worker, library, monkeypatch
) -> None:
    """`_after_probe`'s per-job guard is right but arrives too late to be kind: a full
    `/work` would fail three hundred backfill jobs one at a time, burning an attempt
    each and filling §9.1's Queue page with identical failures. The honest answer is
    "the disk is full, nothing can run"."""
    import shutil

    item_id, _ = library
    job_id = queue_job(item_id)

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: type("U", (), {"free": 1 << 20, "total": 1 << 30})()
    )
    assert worker.poll_once() is False, "nothing was claimed"

    job = job_row(job_id)
    assert job.state == "queued", "the queue survives intact"
    assert job.attempts == 0, "and no attempt was burned"


def test_the_queue_drains_once_space_appears(worker, library, monkeypatch) -> None:
    import shutil

    item_id, _ = library
    queue_job(item_id)
    real = shutil.disk_usage
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: type("U", (), {"free": 1 << 20, "total": 1 << 30})()
    )
    assert worker.poll_once() is False

    monkeypatch.setattr(shutil, "disk_usage", real)
    assert worker.poll_once() is True


def test_the_gate_can_be_turned_off(worker, library, monkeypatch) -> None:
    import shutil

    from vidcleaner.settings_store import save_settings

    item_id, _ = library
    queue_job(item_id)
    with session_scope() as session:
        save_settings(session, {"min_free_gib": 0})
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: type("U", (), {"free": 0, "total": 1 << 30})()
    )
    assert worker.poll_once() is True, "0 disables the gate"


def test_a_moved_file_is_re_resolved_and_the_retry_succeeds(worker, library, monkeypatch) -> None:
    """§6 end to end: the path vanished, the arr said where it went, the retry worked.

    Before M5 the retry ran against the identical path a minute later, so the one
    allowed attempt was guaranteed to be wasted and every moved file ended `stale`.
    """
    item_id, source = library
    moved = source.with_name("S01E01 - Renamed.mkv")
    source.rename(moved)
    job_id = queue_job(item_id)

    from vidcleaner.worker import resolve as resolve_mod
    from vidcleaner.worker import runner as runner_mod

    def fake_reresolve(session, item, _title, _integrations):
        item.path = str(moved)
        session.flush()
        return resolve_mod.Resolution("moved", path=str(moved), detail="moved")

    monkeypatch.setattr(runner_mod, "_integrations_for", lambda _s: None)
    monkeypatch.setattr(resolve_mod, "reresolve_path", fake_reresolve)

    worker.poll_once()
    assert job_row(job_id).state == "queued", "requeued because the file actually moved"
    assert any("asked the arr where the file went" in m for m in timeline(job_id))

    with session_scope() as session:
        session.get(Job, job_id).retry_at = None
    assert worker.poll_once() is True
    assert job_row(job_id).state == "done", "the retry ran against the new path"


def test_a_stale_job_leaves_the_item_stale(worker, library) -> None:
    """`pending` means "we intend to clean it" and `sync.backfill_title` does not skip
    it, so the item would be instantly re-enqueueable against a path that is gone."""
    item_id, source = library
    source.unlink()
    job_id = queue_job(item_id)

    worker.poll_once()
    with session_scope() as session:
        session.get(Job, job_id).retry_at = None
    worker.poll_once()

    assert job_row(job_id).state == "stale"
    with session_scope() as session:
        assert session.get(MediaItem, item_id).status == "stale"
