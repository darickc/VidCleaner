"""Reclaiming `/work`.

PLAN.md never mentions this, and it is the failure that would actually take the box
down: `out.mkv` is source-sized -- 4.57 GiB in the M1 demo -- plus roughly 110 MB per
hour of `audio.wav`, and §10 puts `/work` on a cache SSD. Twenty episodes fills it.

The directory cannot simply be deleted, because §6 step 10 puts the UI's snippet audio
in `snippets/` and M4 reads `detections.json` back from here. So a finished job is
*pruned* -- the big regenerable files go, the small evidence stays -- and whole
directories are removed only once the job is well past.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.config import Settings
from vidcleaner.db.constants import TERMINAL_STATES
from vidcleaner.db.models import Job
from vidcleaner.db.session import utcnow
from vidcleaner.logging import get_logger
from vidcleaner.pipeline.workspace import Workspace

__all__ = ["PruneReport", "collect_work_dirs", "prune_work_dir"]

log = get_logger(__name__)

#: Regenerable, and by far the biggest things in the directory.
PRUNE_FILES = ("out.mkv", "audio.wav", "graph.txt")
PRUNE_DIRS = ("subs", "redacted")
#: How long a terminal job's directory survives, so a failure can still be diagnosed.
KEEP_DIRS_DAYS = 7


@dataclass(frozen=True, slots=True)
class PruneReport:
    freed_bytes: int = 0
    dirs_removed: int = 0


def prune_work_dir(ws: Workspace) -> int:
    """Drop the big regenerable artifacts of a finished job. Returns bytes freed.

    Kept: `job.json`, the JSON artifacts, `ffmpeg.log`, the stage markers and
    `snippets/`. Everything M4 reads, and everything a bug report needs.
    """
    freed = 0
    for name in PRUNE_FILES:
        path = ws.root / name
        try:
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
        except OSError as exc:  # pragma: no cover - a locked file is not fatal
            log.warning("gc.unlink_failed", path=str(path), error=str(exc))
    for name in PRUNE_DIRS:
        path = ws.root / name
        if not path.is_dir():
            continue
        freed += _size(path)
        shutil.rmtree(path, ignore_errors=True)
    if freed:
        log.info("gc.pruned", job_id=ws.job_id, freed_mib=round(freed / 2**20, 1))
    return freed


def collect_work_dirs(
    session: Session, settings: Settings, *, keep_days: int = KEEP_DIRS_DAYS
) -> PruneReport:
    """Remove whole directories for jobs that finished long ago, and orphans.

    An orphan is a directory whose job is not in the database at all -- a CLI run's
    work dir, or a job pruned by a future retention policy. Those are only removed
    once they are older than the same window, so a run in progress is never touched.
    """
    root = settings.work_dir
    if not root.is_dir():
        return PruneReport()

    cutoff = utcnow() - timedelta(days=keep_days)
    terminal = {
        job_id: finished
        for job_id, finished in session.execute(
            select(Job.id, Job.finished_at).where(Job.state.in_(TERMINAL_STATES))
        )
    }

    freed = 0
    removed = 0
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if path.name in terminal:
            finished = terminal[path.name]
            if finished is not None and finished > cutoff:
                continue
        elif _mtime(path) > cutoff:
            # Not a job we know about, and recent: a live CLI run, or a job whose
            # row has not been written yet. Leave it alone.
            continue
        else:
            log.info("gc.orphan_work_dir", path=str(path))
        freed += _size(path)
        shutil.rmtree(path, ignore_errors=True)
        removed += 1

    if removed:
        log.info("gc.collected", dirs=removed, freed_mib=round(freed / 2**20, 1))
    return PruneReport(freed_bytes=freed, dirs_removed=removed)


def _size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:  # pragma: no cover
            continue
    return total


def _mtime(path: Path):
    from datetime import UTC, datetime  # noqa: PLC0415

    try:
        stamp = path.stat().st_mtime
    except OSError:  # pragma: no cover
        return utcnow()
    return datetime.fromtimestamp(stamp, UTC).replace(tzinfo=None)
