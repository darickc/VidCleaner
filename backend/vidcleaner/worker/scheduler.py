"""Periodic work the worker owns.

Split by resource (see the Decision Log): this process owns anything that needs queue
idleness or the volumes -- stale recovery, `/work` collection, §6's idle-gated audit
pass -- while the api owns the hourly arr sync, because a timer here fires however
late the current ffmpeg or STT stage happens to be.

`tick()` is called only when `poll_once` found no work, so every task inherits "runs
when the queue is idle" for free -- which is exactly what §6 requires of the audit
pass and costs the others nothing.

Last-run times are in memory. A restart re-running one of these is harmless and much
cheaper than a table to persist them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sqlalchemy import select

from vidcleaner.config import Settings, get_settings
from vidcleaner.db.models import Backup, Job, MediaItem, Title
from vidcleaner.db.queries import evidence_job_ids
from vidcleaner.db.session import session_scope
from vidcleaner.logging import get_logger
from vidcleaner.worker import claim as queue
from vidcleaner.worker.gc import collect_work_dirs

__all__ = ["PeriodicTask", "Scheduler", "enqueue_audit_pass", "promote_audits"]

log = get_logger(__name__)

RECOVER_INTERVAL_S: Final = 30.0
GC_INTERVAL_S: Final = 3600.0
RETENTION_INTERVAL_S: Final = 3600.0
AUDIT_INTERVAL_S: Final = 300.0
#: How many recent audits `promote_audits` looks at per tick.
AUDIT_PROMOTE_SCAN: Final = 50
#: A file whose audit keeps failing must not be retried forever.
MAX_AUDIT_FAILURES: Final = 2


@dataclass(frozen=True, slots=True)
class PeriodicTask:
    name: str
    interval_s: float
    run: Callable[[Settings], None]


def _recover_stale(_settings: Settings) -> None:
    with session_scope() as session:
        queue.recover_stale(session)


def _collect(settings: Settings) -> None:
    with session_scope() as session:
        collect_work_dirs(session, settings)


def _retention(settings: Settings) -> None:
    """§13's retention, in the order that makes it safe.

    `reconcile_backups` runs **first** so a file whose row was lost to a crash between
    the rename and the commit is adopted (as `orphaned`, with a clock) before anything
    deletes by row -- otherwise that file would sit in `/backups` forever, since the
    purge deliberately never walks the directory. It was CLI-only until now.
    """
    from vidcleaner.pipeline.persist import reconcile_backups  # noqa: PLC0415
    from vidcleaner.settings_store import load_settings  # noqa: PLC0415
    from vidcleaner.worker.purge import purge_backups  # noqa: PLC0415

    with session_scope() as session:
        days = load_settings(session).backup_retention_days
        reconcile_backups(session, settings.backups_dir, retention_days=days)
        if days:
            # 0 means keep forever, and `purge_after` is NULL for those rows -- but
            # rows written while the setting was non-zero still carry a date, so the
            # switch has to be checked here too or turning retention off would not
            # actually stop the deletions.
            purge_backups(session, settings)


def enqueue_audit_pass(settings: Settings | None = None) -> list[str]:
    """§6's audit pass, phase 1: a full-file re-check that touches nothing.

    Enqueued as a **dry run against the backup original** (see
    `worker.spec.audit_source`), which buys four things the obvious "force a reclean"
    shape does not: the library stays byte-identical for the whole ~30-minute
    transcription rather than only at the end; ``probe.already_clean`` is False without
    ``force``, so a killed job resumes from its stage markers instead of restarting;
    the un-redacted original subtitles are read; and a failure costs nothing.

    Whether it then re-renders is :func:`promote_audits`' decision, recomputed from
    rows rather than remembered here.
    """
    with session_scope() as session:
        from vidcleaner.settings_store import load_settings  # noqa: PLC0415

        app = load_settings(session)
        if app.audit_pass == "off":
            return []
        if app.audit_pass == "idle" and _queue_busy(session):
            return []

        queued: list[str] = []
        for item in session.scalars(
            select(MediaItem).where(MediaItem.status == "clean").order_by(MediaItem.cleaned_at)
        ):
            if not _auditable(session, item, app):
                continue
            result = queue.enqueue(
                session,
                media_item_id=item.id,
                trigger="audit",
                stt_mode="audit",
                dry_run=True,
            )
            if result.created:
                queued.append(result.job_id)
                log.info("audit.enqueued", media_item_id=item.id, job_id=result.job_id)
            break  # one per tick: the point is that it never competes with real work
        return queued


def _queue_busy(session) -> bool:
    """Anything not yet terminal. Not redundant with the idle poll that got us here:
    a job can be `queued` with a future `retry_at`, or claimed by another worker."""
    return (
        session.scalars(
            select(Job.id).where(Job.state.notin_(queue.TERMINAL_STATES)).limit(1)
        ).first()
        is not None
    )


def _auditable(session, item: MediaItem, app) -> bool:
    """Every reason to skip an item, cheapest first."""
    title = session.get(Title, item.title_id)
    if title is None or not title.enabled or title.arr_id is None or title.arr_id < 0:
        return False

    job = session.get(Job, item.last_job_id) if item.last_job_id else None
    if job is None or job.stt_mode != "windowed":
        # A full pass over a file that already had one would find the same thing at
        # the same cost.
        return False

    # §6's premise is re-checking the *original* audio, and after a clean the library
    # file's default audio stream is the muted Clean track -- so with no backup there
    # is simply nothing to audit against. Enqueueing anyway would burn attempts and
    # clutter the Queue page with a job that cannot succeed.
    backup = session.scalars(
        select(Backup)
        .where(
            Backup.media_item_id == item.id,
            Backup.state == "kept",
            Backup.backup_path.notlike("%.srt"),
        )
        .order_by(Backup.created_at.desc(), Backup.id.desc())
    ).first()
    if backup is None:
        return False
    if not Path(backup.backup_path).is_file():
        # Self-healing, and far cheaper than `reconcile_backups`, which rglobs the
        # whole backups tree to learn the same thing about one row.
        backup.state = "purged"
        session.flush()
        log.warning("audit.backup_missing", media_item_id=item.id, path=backup.backup_path)
        return False

    # §13 lists a full pass over a long film as the top CPU risk, and M2 exempted
    # explicit modes from `stt_full_max_hours` on the grounds that the cap must not
    # stop a user who asked for one. Nobody asked here -- the scheduler volunteered --
    # so the cap applies. A 3-hour film would otherwise get a ~6-hour audit.
    if app.stt_full_max_hours and item.duration and item.duration > app.stt_full_max_hours * 3600.0:
        return False

    return _covering_audit(session, item, backup) is None


def _covering_audit(session, item: MediaItem, backup: Backup):
    """A completed audit that still describes this file, if there is one.

    The cache key is ``(item, backup fingerprint, profile hash)``, all of it already
    recorded -- ``persist_run`` writes ``jobs.source_fingerprint`` from the probe, and
    `swap._safe_fingerprint` computes ``backups.sha1_prefix`` with the same function.
    So no new column, and every case falls out right:

    * a reprocess restores the same original, so the new backup has the same
      fingerprint -- **no** re-audit, which is correct: the same audio would yield the
      same words for another 30 minutes;
    * a word-list edit changes the profile hash -- re-audit;
    * a Sonarr upgrade replaces the file, so the next backup differs -- re-audit.

    This replaces M4's `_has_audit`, which matched **any** audit job in any state and so
    gave each item exactly one attempt for the lifetime of the database -- an attempt
    that, before this milestone, always died at `probe`.
    """
    failures = 0
    for job in session.scalars(
        select(Job)
        .where(Job.media_item_id == item.id, Job.trigger == "audit", Job.dry_run.is_(True))
        .order_by(Job.created_at.desc())
    ):
        if job.state == "failed":
            failures += 1
            continue
        if job.state != "done":
            continue
        if backup.sha1_prefix and job.source_fingerprint != backup.sha1_prefix:
            continue
        if _snapshot_hash(job) != _current_hash(session, item):
            continue
        return job
    # A poison file must not be retried forever.
    return object() if failures >= MAX_AUDIT_FAILURES else None


def _snapshot_hash(job: Job) -> str:
    try:
        return str(json.loads(job.profile_snapshot_json or "{}").get("profile_hash", ""))
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return ""


def _current_hash(session, item: MediaItem) -> str:
    from vidcleaner.matching.profile import matcher_for  # noqa: PLC0415

    title = session.get(Title, item.title_id)
    return matcher_for(
        session,
        title_id=item.title_id,
        item_id=item.id,
        profile_id=title.profile_id if title else None,
    ).profile_hash


def promote_audits(settings: Settings | None = None) -> list[str]:
    """§6's "if new hits appear, it re-renders" -- phase 2.

    **Recomputed, not remembered.** The decision is `compare(prior, audit rows)`, and
    both sides are in the database: a worker dying between phase 1 and this loses
    nothing, and running it twice enqueues nothing the second time. That matches the
    scheduler's existing stance on last-run times, and it needs no column.

    It terminates because phase 2 becomes the item's evidence job with the merged set,
    so the next comparison finds nothing new.
    """
    from vidcleaner.pipeline.audit import AuditOptions, compare  # noqa: PLC0415
    from vidcleaner.pipeline.persist import detections_for_job  # noqa: PLC0415
    from vidcleaner.settings_store import load_settings  # noqa: PLC0415

    with session_scope() as session:
        app = load_settings(session)
        if app.audit_pass == "off":
            return []

        queued: list[str] = []
        for job in session.scalars(
            select(Job)
            .where(Job.trigger == "audit", Job.dry_run.is_(True), Job.state == "done")
            .order_by(Job.created_at.desc())
            .limit(AUDIT_PROMOTE_SCAN)
        ):
            item = session.get(MediaItem, job.media_item_id)
            if item is None or item.status != "clean":
                continue
            evidence = evidence_job_ids(session, [item]).get(item.id)
            if evidence is None or evidence == job.id:
                continue
            if _newer_real_run(session, item, job):
                continue  # phase 2 already ran, or the file was recleaned since
            comparison = compare(
                detections_for_job(session, evidence),
                detections_for_job(session, job.id),
                opts=AuditOptions(min_confidence=app.audit_min_confidence),
            )
            if not comparison.should_render:
                continue
            result = queue.enqueue(
                session,
                media_item_id=item.id,
                trigger="audit",
                stt_mode="audit",
                force=True,
            )
            if result.created:
                queued.append(result.job_id)
                log.info(
                    "audit.promoted",
                    media_item_id=item.id,
                    job_id=result.job_id,
                    **comparison.summary,
                )
            break  # one per tick, like phase 1
        return queued


def _newer_real_run(session, item: MediaItem, audit: Job) -> bool:
    """Has anything actually touched the library since this audit finished?"""
    return (
        session.scalars(
            select(Job.id)
            .where(
                Job.media_item_id == item.id,
                Job.dry_run.is_(False),
                Job.state == "done",
                Job.created_at > audit.created_at,
            )
            .limit(1)
        ).first()
        is not None
    )


def _audit(settings: Settings) -> None:
    enqueue_audit_pass(settings)
    promote_audits(settings)


@dataclass
class Scheduler:
    settings: Settings = field(default_factory=get_settings)
    tasks: tuple[PeriodicTask, ...] = ()
    clock: Callable[[], float] = time.monotonic
    _last: dict[str, float] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.tasks:
            self.tasks = (
                PeriodicTask("recover_stale", RECOVER_INTERVAL_S, _recover_stale),
                PeriodicTask("audit", AUDIT_INTERVAL_S, _audit),
                PeriodicTask("gc_work_dirs", GC_INTERVAL_S, _collect),
                PeriodicTask("retention", RETENTION_INTERVAL_S, _retention),
            )

    def tick(self, only: tuple[str, ...] | None = None) -> list[str]:
        """Run whatever is due. Returns the names that ran, for tests and logging.

        ``only`` restricts the run to named tasks, which is what lets `Worker.run`
        call the audit on the **busy** path too. Without that, ``audit_pass="always"``
        was indistinguishable from ``"idle"``: `tick` is otherwise reached only when
        `poll_once` found nothing, so the outer gate already required an idle queue and
        the setting's third value did nothing.
        """
        now = self.clock()
        ran: list[str] = []
        for task in self.tasks:
            if only is not None and task.name not in only:
                continue
            last = self._last.get(task.name)
            if last is not None and now - last < task.interval_s:
                continue
            self._last[task.name] = now
            try:
                task.run(self.settings)
            except Exception:
                log.exception("scheduler.task_failed", task=task.name)
            ran.append(task.name)
        return ran
