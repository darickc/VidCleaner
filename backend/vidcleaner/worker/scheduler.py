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

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select

from vidcleaner.config import Settings, get_settings
from vidcleaner.db.models import Job, MediaItem, Title
from vidcleaner.db.session import session_scope
from vidcleaner.logging import get_logger
from vidcleaner.worker import claim as queue
from vidcleaner.worker.gc import collect_work_dirs

__all__ = ["PeriodicTask", "Scheduler", "enqueue_audit_pass"]

log = get_logger(__name__)

RECOVER_INTERVAL_S: Final = 30.0
GC_INTERVAL_S: Final = 3600.0
AUDIT_INTERVAL_S: Final = 300.0


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


def enqueue_audit_pass(settings: Settings | None = None) -> list[str]:
    """§6's audit pass: a low-priority full-file re-check of a windowed job.

    Enqueued only when nothing else is queued, and only for items whose last job
    actually ran in windowed mode -- a full pass over a file that already had one
    would find exactly the same thing at the same cost.
    """
    with session_scope() as session:
        from vidcleaner.settings_store import load_settings  # noqa: PLC0415

        mode = load_settings(session).audit_pass
        if mode == "off":
            return []
        if mode == "idle" and _queue_busy(session):
            return []

        queued: list[str] = []
        for item in session.scalars(
            select(MediaItem).where(MediaItem.status == "clean").order_by(MediaItem.cleaned_at)
        ):
            title = session.get(Title, item.title_id)
            if title is None or not title.enabled or title.arr_id is None or title.arr_id < 0:
                continue
            job = session.get(Job, item.last_job_id) if item.last_job_id else None
            if job is None or job.stt_mode != "windowed":
                continue
            if _has_audit(session, item.id):
                continue
            result = queue.enqueue(
                session, media_item_id=item.id, trigger="audit", stt_mode="audit"
            )
            if result.created:
                queued.append(result.job_id)
            break  # one per tick: the point is that it never competes with real work
        return queued


def _queue_busy(session) -> bool:
    return (
        session.scalars(
            select(Job.id).where(Job.state.notin_(queue.TERMINAL_STATES)).limit(1)
        ).first()
        is not None
    )


def _has_audit(session, media_item_id: int) -> bool:
    return (
        session.scalars(
            select(Job.id)
            .where(Job.media_item_id == media_item_id, Job.trigger == "audit")
            .limit(1)
        ).first()
        is not None
    )


def _audit(settings: Settings) -> None:
    enqueue_audit_pass(settings)


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
            )

    def tick(self) -> list[str]:
        """Run whatever is due. Returns the names that ran, for tests and logging."""
        now = self.clock()
        ran: list[str] = []
        for task in self.tasks:
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
