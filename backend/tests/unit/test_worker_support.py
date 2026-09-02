"""Progress arithmetic, the monitor thread, the timeline, and retry policy.

No ffmpeg and no torch. The monitor tests use the real database (the file-backed
`migrated` fixture) and drive `beat()` by hand, so they exercise the actual writes
without sleeping.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from vidcleaner.config import Settings
from vidcleaner.db.models import Job, JobLog, MediaItem, Title
from vidcleaner.db.session import session_scope
from vidcleaner.pipeline.stages import (
    DRY_RUN_STAGES,
    M3_STAGES,
    StageError,
    StaleSourceError,
    SwapBrokenError,
)
from vidcleaner.worker.claim import MAX_ATTEMPTS, cancel, claim_next, enqueue
from vidcleaner.worker.heartbeat import JobMonitor
from vidcleaner.worker.joblog import Timeline
from vidcleaner.worker.policy import BACKOFF_S, classify
from vidcleaner.worker.progress import (
    STAGE_WEIGHTS_FULL,
    STAGE_WEIGHTS_WINDOWED,
    ProgressTracker,
)

# -------------------------------------------------------------------- progress


def test_progress_is_monotonic_and_ends_at_one_hundred() -> None:
    tracker = ProgressTracker(M3_STAGES)
    seen = [tracker.absolute(stage, f) for stage in M3_STAGES for f in (0.0, 0.5, 1.0)]
    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(100.0)


def test_a_stage_reporting_zero_does_not_drag_the_bar_back() -> None:
    tracker = ProgressTracker(M3_STAGES)
    after_transcribe = tracker.complete("transcribe")
    # Re-reporting the stage we just finished, which whisperX's per-batch callback
    # does when a later batch restarts at a low fraction.
    assert tracker.absolute("transcribe", 0.0) == pytest.approx(after_transcribe)
    # A later stage at 0% is still further along, because the stages between are done.
    assert tracker.absolute("render", 0.0) >= after_transcribe


def test_a_dry_run_normalises_over_its_own_stages() -> None:
    """Five stages, not ten: a dry run that finishes must read 100%."""
    tracker = ProgressTracker(DRY_RUN_STAGES)
    assert tracker.complete("detect") == pytest.approx(100.0)


def test_a_resumed_job_starts_from_its_markers() -> None:
    """`run_stage` skips a completed stage without calling progress at all."""
    fresh = ProgressTracker(M3_STAGES)
    resumed = ProgressTracker(M3_STAGES, completed=("probe", "extract", "subtitles", "transcribe"))
    assert resumed.value > 60.0
    assert resumed.value > fresh.value


def test_full_mode_weights_transcribe_far_higher() -> None:
    windowed = ProgressTracker(M3_STAGES, weights=STAGE_WEIGHTS_WINDOWED)
    full = ProgressTracker(M3_STAGES, weights=STAGE_WEIGHTS_FULL)
    assert full.complete("subtitles") < windowed.complete("subtitles")


def test_reweighting_mid_run_keeps_the_bar_from_going_backwards() -> None:
    """The promotion to a full pass is decided inside the transcribe stage."""
    tracker = ProgressTracker(M3_STAGES)
    before = tracker.complete("subtitles")
    tracker.reweight(STAGE_WEIGHTS_FULL)
    assert tracker.absolute("transcribe", 0.0) == pytest.approx(before)


def test_an_unplanned_stage_is_ignored() -> None:
    tracker = ProgressTracker(DRY_RUN_STAGES)
    tracker.complete("detect")
    assert tracker.absolute("swap", 1.0) == pytest.approx(100.0)


# --------------------------------------------------------------------- monitor


def queued_job(status: str = "pending") -> str:
    with session_scope() as session:
        title = Title(kind="series", arr_id=1, title="Show", enabled=True)
        session.add(title)
        session.flush()
        item = MediaItem(
            title_id=title.id,
            kind="episode",
            season=1,
            episode=1,
            path="/media/tv/Show/S01E01.mkv",
            status=status,
        )
        session.add(item)
        session.flush()
        return enqueue(session, media_item_id=item.id, trigger="webhook").job_id


def monitor_for(job_id: str, settings: Settings, worker_id: str = "w1") -> JobMonitor:
    return JobMonitor(
        job_id=job_id,
        worker_id=worker_id,
        tracker=ProgressTracker(M3_STAGES),
        settings=settings,
    )


def test_the_monitor_writes_stage_state_and_progress(migrated: Settings) -> None:
    job_id = queued_job()
    claim_next(worker_id="w1", settings=migrated)
    monitor = monitor_for(job_id, migrated)
    monitor.set_stage("transcribe", "transcribing")
    monitor.report("transcribe", 0.5)

    assert monitor.beat() is True
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.state == "transcribing"
        assert job.stage == "transcribe"
        assert 0 < job.progress_pct < 100
        assert job.heartbeat is not None


def test_a_stage_transition_is_published_immediately(migrated: Settings) -> None:
    """A job shorter than one tick would otherwise still read `probing` when done,
    and a crashed job's state would be no guide to where it stopped."""
    job_id = queued_job()
    claim_next(worker_id="w1", settings=migrated)
    monitor = monitor_for(job_id, migrated)
    monitor.set_stage("detect", "detecting")  # no beat() call
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert (job.state, job.stage) == ("detecting", "detect")


def test_the_monitor_stops_when_the_job_is_cancelled(migrated: Settings) -> None:
    job_id = queued_job()
    claim_next(worker_id="w1", settings=migrated)
    monitor = monitor_for(job_id, migrated)
    monitor.set_stage("probe", "probing")
    assert monitor.beat() is True

    with session_scope() as session:
        cancel(session, job_id, reason="superseded")

    assert monitor.beat() is False
    assert monitor.cancelled
    # `cancel` clears claimed_by, so the heartbeat is fenced out on the same tick.
    assert monitor.reason in {"cancelled", "fenced"}


def test_the_monitor_stops_when_the_job_was_stolen(migrated: Settings) -> None:
    job_id = queued_job()
    claim_next(worker_id="w1", settings=migrated)
    monitor = monitor_for(job_id, migrated)
    with session_scope() as session:
        session.get(Job, job_id).claimed_by = "someone-else"
    assert monitor.beat() is False
    assert monitor.reason == "fenced"


def test_reporting_from_another_thread_while_the_monitor_writes(migrated: Settings) -> None:
    """The regression test for the threading decision: the pipeline calls `report`
    from ffmpeg's pump thread while the monitor thread writes the row."""
    job_id = queued_job()
    claim_next(worker_id="w1", settings=migrated)
    monitor = monitor_for(job_id, migrated)
    monitor.set_stage("render", "rendering")

    stop = threading.Event()

    def spam() -> None:
        n = 0
        while not stop.is_set():
            monitor.report("render", (n % 100) / 100)
            n += 1

    reporter = threading.Thread(target=spam, daemon=True)
    reporter.start()
    try:
        with monitor:
            for _ in range(20):
                assert monitor.beat() is True
    finally:
        stop.set()
        reporter.join(5)

    with session_scope() as session:
        assert session.get(Job, job_id).stage == "render"


def test_the_monitor_thread_starts_and_stops(migrated: Settings) -> None:
    job_id = queued_job()
    claim_next(worker_id="w1", settings=migrated)
    monitor = monitor_for(job_id, migrated)
    monitor.set_stage("probe", "probing")
    with monitor:
        pass
    with session_scope() as session:
        assert session.get(Job, job_id).state == "probing"


# -------------------------------------------------------------------- timeline


def test_the_timeline_buffers_until_flushed(migrated: Settings) -> None:
    job_id = queued_job()
    timeline = Timeline(job_id)
    timeline.info("claimed", worker="w1", attempts=1)
    timeline.warning("subtitles unreliable", offset=-0.16)

    with session_scope() as session:
        assert session.scalars(select(JobLog).where(JobLog.job_id == job_id)).all() != []
        before = len(session.scalars(select(JobLog).where(JobLog.job_id == job_id)).all())

    assert timeline.pending == 2
    assert timeline.flush() == 2
    assert timeline.pending == 0

    with session_scope() as session:
        rows = session.scalars(
            select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.id)
        ).all()
        assert len(rows) == before + 2
        assert "worker=w1" in rows[-2].msg
        assert rows[-1].level == "warning"


def test_flushing_an_empty_timeline_is_free(migrated: Settings) -> None:
    assert Timeline(queued_job()).flush() == 0


def test_the_timeline_never_raises_on_a_bad_job_id(migrated: Settings) -> None:
    """Bookkeeping must not fail a good render -- the foreign key would reject this."""
    timeline = Timeline("no-such-job")
    timeline.info("orphan")
    assert timeline.flush() == 0


def test_the_context_manager_flushes(migrated: Settings) -> None:
    job_id = queued_job()
    with Timeline(job_id) as timeline:
        timeline.info("hello")
    with session_scope() as session:
        msgs = [r.msg for r in session.scalars(select(JobLog).where(JobLog.job_id == job_id))]
        assert "hello" in msgs


# ---------------------------------------------------------------------- policy


def test_render_and_verify_failures_are_terminal() -> None:
    """§6: retrying re-encodes twenty minutes to fail identically."""
    for stage in ("render", "verify", "swap"):
        outcome = classify(stage, StageError(stage, "boom"), attempts=1)
        assert (outcome.verdict, outcome.state, outcome.retry_in_s) == ("terminal", "failed", None)


def test_refresh_failing_after_the_swap_still_leaves_the_job_done() -> None:
    """The library file is already correct; the hourly sync repairs the rest."""
    outcome = classify("refresh", StageError("refresh", "sonarr 503"), attempts=1)
    assert (outcome.verdict, outcome.state) == ("best_effort", "done")


def test_an_early_stage_failure_retries_with_backoff() -> None:
    first = classify("subtitles", StageError("subtitles", "boom"), attempts=1)
    assert (first.verdict, first.state, first.retry_in_s) == ("retry", "queued", BACKOFF_S[0])
    second = classify("subtitles", StageError("subtitles", "boom"), attempts=2)
    assert second.retry_in_s == BACKOFF_S[1]


def test_a_poison_job_is_retired() -> None:
    outcome = classify("extract", StageError("extract", "boom"), attempts=MAX_ATTEMPTS)
    assert (outcome.verdict, outcome.state) == ("terminal", "failed")


def test_a_vanished_source_is_requeued_once_then_stale() -> None:
    """§6's "path vanished": re-resolve via the arr and try again, once."""
    first = classify("probe", StaleSourceError("probe", "source is gone"), attempts=1)
    assert (first.verdict, first.state, first.retry_in_s) == ("stale", "queued", 60.0)
    second = classify("probe", StaleSourceError("probe", "source is gone"), attempts=2)
    assert (second.verdict, second.state) == ("stale", "stale")


def test_a_broken_swap_is_never_retried() -> None:
    outcome = classify("swap", SwapBrokenError("swap", "rollback failed"), attempts=1)
    assert outcome.verdict == "terminal"
    assert "manual" in outcome.detail
