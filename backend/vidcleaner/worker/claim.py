"""Claiming, heartbeating and enqueueing jobs -- PLAN.md §4's queue.

The `jobs` table *is* the queue. Three properties have to hold with the api and the
worker in separate processes:

* **Exactly one worker gets a job.** The claim reads a row and then updates it, which
  under WAL cannot be done in a deferred transaction: the upgrade fails with
  ``SQLITE_BUSY_SNAPSHOT`` immediately, because SQLite does not invoke the busy handler
  for snapshot conflicts. Hence :func:`db.session.immediate_connection`.
* **A dead worker's job comes back.** :func:`recover_stale` requeues anything whose
  heartbeat has lapsed -- with one exception, ``swapping``, which is the only stage
  that mutates the library and therefore the only one a blind re-run can make worse.
* **A live worker keeps its job.** Every write carries ``claimed_by = :me``, so a
  worker whose heartbeat lapsed while it was still alive (a suspended container, an
  NFS stall) discovers on its next tick that the job was taken and stops. That is a
  fencing token with no extra column.

SQLAlchemy Core, not ``text()``: a bound ``datetime`` reaching raw pysqlite goes
through Python 3.12's deprecated default adapter, and hand-formatting it to match
SQLAlchemy's storage format is exactly the kind of detail that works until it doesn't.

One coupling worth stating, because it is invisible until a library gets large: any
transaction that holds the write lock for longer than ``busy_timeout`` (5 s) makes the
*other* process's claim fail with "database is locked". A backfill that enqueues
several hundred rows must therefore be chunked across transactions rather than done in
one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from vidcleaner.config import Settings
from vidcleaner.db.constants import (
    DEFAULT_PRIORITY,
    RUNNING_STATES,
    TERMINAL_STATES,
)
from vidcleaner.db.models import Job, JobLog, MediaItem
from vidcleaner.db.session import get_engine, immediate_connection, utcnow
from vidcleaner.logging import get_logger

__all__ = [
    "HEARTBEAT_INTERVAL_S",
    "HEARTBEAT_TICK_S",
    "MAX_ATTEMPTS",
    "STALE_AFTER_S",
    "Claim",
    "EnqueueResult",
    "StaleJob",
    "cancel",
    "claim_next",
    "enqueue",
    "heartbeat",
    "log_event",
    "recover_stale",
    "release",
    "reprioritize",
    "set_state",
    "should_abort",
]

log = get_logger(__name__)

#: §4's contract: "Heartbeat every 30 s".
HEARTBEAT_INTERVAL_S: Final = 30.0
#: How often the monitor thread actually writes. Smaller than the contract so the UI's
#: progress is smooth; writing more often than promised is always safe.
HEARTBEAT_TICK_S: Final = 5.0
#: Four missed heartbeats. Has to tolerate a slow `/work` volume and §6.1's 300 s
#: stability wait, which is the reason the heartbeat is a thread and not a callback.
STALE_AFTER_S: Final = 4 * HEARTBEAT_INTERVAL_S
#: A job claimed this many times without reaching a terminal state is poison.
MAX_ATTEMPTS: Final = 3

#: Filled in by ``pipeline.swap`` (M3 step 5). Given a job interrupted during the swap,
#: it inspects the intent journal and both paths and answers "requeue" / "done" /
#: "failed". Until it is set, an interrupted swap is always ``failed`` -- the safe
#: answer, because the alternative is re-running renames over an unknown state.
SWAP_RECONCILER: Any = None


@dataclass(frozen=True, slots=True)
class Claim:
    """The claimed job's parameters. Everything else the runner reads from the ORM."""

    job_id: str
    media_item_id: int
    trigger: str
    priority: int
    attempts: int
    stt_mode: str
    dry_run: bool
    force: bool
    work_dir: str | None


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: str
    created: bool
    reason: str
    superseded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StaleJob:
    job_id: str
    state: str
    outcome: str
    """``requeued`` | ``failed`` | ``retired``."""


# ------------------------------------------------------------------ claiming


def claim_next(
    *,
    worker_id: str,
    settings: Settings | None = None,
    now: datetime | None = None,
    busy_timeout_ms: int | None = None,
) -> Claim | None:
    """Take the highest-priority runnable job, or return ``None``.

    ``state`` is set to ``probing`` here, which is briefly a lie for a job resuming
    from a later marker. Resolving it properly would mean reading the work dir's
    markers *inside* this write transaction, and holding the write lock for filesystem
    I/O is how the other process's claim starts failing with "database is locked". The
    runner corrects the state from the markers before it runs anything, so the wrong
    value is never observable for longer than one poll iteration.
    """
    now = now or utcnow()
    try:
        with immediate_connection(settings, busy_timeout_ms=busy_timeout_ms) as connection:
            row = connection.execute(
                select(
                    Job.id,
                    Job.media_item_id,
                    Job.trigger,
                    Job.priority,
                    Job.attempts,
                    Job.stt_mode,
                    Job.dry_run,
                    Job.force,
                    Job.work_dir,
                )
                .where(
                    Job.state == "queued",
                    or_(Job.retry_at.is_(None), Job.retry_at <= now),
                )
                .order_by(Job.priority.asc(), Job.created_at.asc())
                .limit(1)
            ).first()
            if row is None:
                return None

            # The `state == "queued"` guard is redundant under BEGIN IMMEDIATE and is
            # kept anyway: it makes the claim safe if the locking is ever relaxed, and
            # it turns "someone else got there first" into an assertable rowcount.
            result = connection.execute(
                update(Job)
                .where(Job.id == row.id, Job.state == "queued")
                .values(
                    state="probing",
                    stage=None,
                    claimed_by=worker_id,
                    heartbeat=now,
                    started_at=func.coalesce(Job.started_at, now),
                    attempts=Job.attempts + 1,
                    error=None,
                    retry_at=None,
                    finished_at=None,
                )
            )
            if result.rowcount != 1:  # pragma: no cover - needs a relaxed lock to hit
                return None
    except OperationalError as exc:
        # The write lock was held past the busy timeout. Not a job failure; the next
        # poll will try again.
        log.warning("claim.locked", worker_id=worker_id, error=str(exc)[:200])
        return None

    claim = Claim(
        job_id=row.id,
        media_item_id=row.media_item_id,
        trigger=row.trigger,
        priority=row.priority,
        attempts=row.attempts + 1,
        stt_mode=row.stt_mode or "windowed",
        dry_run=bool(row.dry_run),
        force=bool(row.force),
        work_dir=row.work_dir,
    )
    log.info(
        "claim.taken",
        job_id=claim.job_id,
        worker_id=worker_id,
        trigger=claim.trigger,
        attempts=claim.attempts,
    )
    return claim


def heartbeat(
    *,
    job_id: str,
    worker_id: str,
    state: str | None = None,
    stage: str | None = None,
    progress_pct: float | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> bool:
    """Refresh the heartbeat, and optionally the observable progress.

    Returns ``False`` when the row no longer belongs to ``worker_id`` -- stolen by
    stale recovery, or the job was cancelled and closed out. The caller must stop: two
    processes running one job is the failure this prevents.

    A plain transaction, not ``immediate_connection``: this is a blind ``UPDATE`` with
    no read to protect, so ``busy_timeout`` alone is the right amount of care.
    """
    values: dict[str, Any] = {"heartbeat": now or utcnow()}
    if state is not None:
        values["state"] = state
    if stage is not None:
        values["stage"] = stage
    if progress_pct is not None:
        values["progress_pct"] = round(progress_pct, 2)
    try:
        with get_engine(settings).begin() as connection:
            result = connection.execute(
                update(Job).where(Job.id == job_id, Job.claimed_by == worker_id).values(**values)
            )
    except OperationalError as exc:
        # A missed heartbeat is survivable (STALE_AFTER_S is four of them); concluding
        # the job was lost because the database was momentarily busy is not.
        log.warning("heartbeat.locked", job_id=job_id, error=str(exc)[:200])
        return True
    return result.rowcount == 1


def should_abort(session: Session, job_id: str) -> bool:
    """Has someone closed this job out from under us? Checked between stages."""
    state = session.scalars(select(Job.state).where(Job.id == job_id)).first()
    return state is None or state in TERMINAL_STATES


# ------------------------------------------------------------------- recovery


def recover_stale(
    session: Session,
    *,
    stale_after_s: float = STALE_AFTER_S,
    now: datetime | None = None,
) -> list[StaleJob]:
    """Requeue jobs whose worker died. Run at startup and on every idle tick.

    ``swapping`` is never requeued blind: it is the only stage that renames library
    files, so a crash between ``rename(original -> backup)`` and
    ``rename(staged -> final)`` leaves a state that a fresh run could make worse.
    Such a job goes to ``failed`` with an actionable error unless
    :data:`SWAP_RECONCILER` can say otherwise.
    """
    now = now or utcnow()
    cutoff = now - timedelta(seconds=stale_after_s)
    jobs = session.scalars(
        select(Job).where(
            Job.state.in_(RUNNING_STATES),
            or_(Job.heartbeat.is_(None), Job.heartbeat < cutoff),
        )
    ).all()

    recovered: list[StaleJob] = []
    for job in jobs:
        was = job.state
        if was == "swapping":
            outcome = _recover_swap(session, job)
        elif job.attempts >= MAX_ATTEMPTS:
            _terminate(job, error=f"abandoned in {was} after {job.attempts} attempts", now=now)
            outcome = "retired"
        else:
            job.state = "queued"
            job.claimed_by = None
            job.heartbeat = None
            job.progress_pct = 0.0
            job.error = None
            outcome = "requeued"

        log_event(session, job.id, f"recovered from {was}: {outcome}", level="warning")
        recovered.append(StaleJob(job.id, was, outcome))
        log.warning("recover.stale", job_id=job.id, was=was, outcome=outcome)

    if recovered:
        session.flush()
    return recovered


def _recover_swap(session: Session, job: Job) -> str:
    if SWAP_RECONCILER is None:
        _terminate(
            job,
            error="interrupted during swap; the library needs reconciliation before a retry",
        )
        return "failed"
    verdict = SWAP_RECONCILER(session, job)
    if verdict == "requeue":
        job.state = "queued"
        job.claimed_by = None
        job.heartbeat = None
        job.error = None
        return "requeued"
    if verdict == "done":
        job.state = "done"
        job.claimed_by = None
        job.progress_pct = 100.0
        job.finished_at = utcnow()
        return "retired"
    _terminate(job, error="interrupted during swap; reconciliation could not resolve it")
    return "failed"


def _terminate(job: Job, *, error: str, now: datetime | None = None) -> None:
    job.state = "failed"
    job.claimed_by = None
    job.heartbeat = None
    job.error = error
    job.finished_at = now or utcnow()


# ------------------------------------------------------------------ enqueueing


def enqueue(
    session: Session,
    *,
    media_item_id: int,
    trigger: str,
    priority: int | None = None,
    stt_mode: str = "windowed",
    dry_run: bool = False,
    force: bool = False,
    dedupe_window_s: float = 60.0,
    supersede: bool = False,
    now: datetime | None = None,
) -> EnqueueResult:
    """Queue work for one media item, at most one live job at a time.

    ``supersede`` is §6.0's upgrade path: the running job is cleaning a file that no
    longer exists, so it is cancelled and replaced.

    The dedupe window is §6.0's "dedupe by path within 60 s". Note that it does *not*
    collapse a season pack -- see the Decision Log; a season-pack import fires one
    ``Download`` per episode *file*, so every event names a different path. What it
    collapses is a duplicate delivery of one event, and a multi-episode file, whose
    several ``Download`` events all share one ``episodeFile``.
    """
    now = now or utcnow()
    priority = DEFAULT_PRIORITY.get(trigger, 100) if priority is None else priority

    live = _live_job(session, media_item_id)
    if live is not None:
        if not supersede:
            age_s = (now - live.created_at).total_seconds() if live.created_at else 0.0
            reason = (
                "deduped"
                if live.state == "queued" and age_s <= dedupe_window_s
                else "already_active"
            )
            return EnqueueResult(live.id, created=False, reason=reason)
        cancel(session, live.id, reason="superseded")
        superseded: tuple[str, ...] = (live.id,)
    else:
        superseded = ()

    job = Job(
        id=str(uuid.uuid4()),
        media_item_id=media_item_id,
        trigger=trigger,
        priority=priority,
        state="queued",
        stt_mode=stt_mode,
        dry_run=dry_run,
        force=force,
        created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # The partial unique index fired: another process inserted between our read
        # and our write. It won; adopt its job rather than failing the caller.
        existing = _live_job(session, media_item_id)
        if existing is None:  # pragma: no cover - the index only fires when one exists
            raise
        return EnqueueResult(existing.id, created=False, reason="raced")

    item = session.get(MediaItem, media_item_id)
    if item is not None and item.status not in ("clean", "already_clean"):
        item.status = "queued"

    log_event(session, job.id, f"queued ({trigger}, priority {priority})")
    session.flush()
    log.info(
        "enqueue.created",
        job_id=job.id,
        media_item_id=media_item_id,
        trigger=trigger,
        priority=priority,
        superseded=list(superseded),
    )
    return EnqueueResult(
        job.id,
        created=True,
        reason="superseded" if superseded else "created",
        superseded=superseded,
    )


def _live_job(session: Session, media_item_id: int) -> Job | None:
    return session.scalars(
        select(Job)
        .where(Job.media_item_id == media_item_id, Job.state.notin_(TERMINAL_STATES))
        .order_by(Job.created_at.desc())
    ).first()


# ------------------------------------------------------------------ transitions


def set_state(
    session: Session,
    *,
    job_id: str,
    state: str,
    stage: str | None = None,
    error: str | None = None,
    progress_pct: float | None = None,
    retry_at: datetime | None = None,
) -> bool:
    job = session.get(Job, job_id)
    if job is None:
        return False
    job.state = state
    if stage is not None:
        job.stage = stage
    if error is not None:
        job.error = error
    if progress_pct is not None:
        job.progress_pct = round(progress_pct, 2)
    if retry_at is not None:
        job.retry_at = retry_at
    if state in TERMINAL_STATES:
        job.claimed_by = None
        job.heartbeat = None
        job.finished_at = utcnow()
    session.flush()
    return True


def release(
    session: Session,
    *,
    job_id: str,
    retry_at: datetime | None = None,
    error: str | None = None,
) -> bool:
    """Put a claimed job back on the queue, optionally not before ``retry_at``."""
    job = session.get(Job, job_id)
    if job is None:
        return False
    job.state = "queued"
    job.claimed_by = None
    job.heartbeat = None
    job.retry_at = retry_at
    job.error = error
    job.finished_at = None
    session.flush()
    log.info("job.released", job_id=job_id, retry_at=retry_at.isoformat() if retry_at else None)
    return True


def cancel(session: Session, job_id: str, *, reason: str = "user") -> bool:
    """Mark a job cancelled. A running worker notices on its next heartbeat tick."""
    job = session.get(Job, job_id)
    if job is None or job.state in TERMINAL_STATES:
        return False
    job.state = "cancelled"
    job.error = reason
    job.claimed_by = None
    job.heartbeat = None
    job.finished_at = utcnow()
    item = session.get(MediaItem, job.media_item_id)
    if item is not None and item.status in ("queued", "processing"):
        item.status = "pending"
    log_event(session, job_id, f"cancelled ({reason})", level="warning")
    session.flush()
    log.info("job.cancelled", job_id=job_id, reason=reason)
    return True


def reprioritize(session: Session, job_id: str, priority: int) -> bool:
    job = session.get(Job, job_id)
    if job is None or job.state != "queued":
        return False
    job.priority = priority
    session.flush()
    return True


def log_event(session: Session, job_id: str, msg: str, *, level: str = "info") -> None:
    """One row on the §9.1/§9.4 job timeline. ffmpeg stderr goes to the file log."""
    session.add(JobLog(job_id=job_id, level=level, msg=msg[:2000]))
