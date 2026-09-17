"""Move an existing install's originals into the visible backups directory.

The backups directory used to be `/media/.vidcleaner-backups`. A dot-prefixed name
hides the one directory in the install that grows by the size of everything you
clean, which makes "I was tidying the drive and did not see it" the expected
outcome rather than the unlucky one -- and deleting it by hand is not harmless: it
makes every clean permanent, disables every restore, and leaves §6's audit pass with
nothing to compare against. So the default is now `/media/VidCleaner-Backups`, and
this moves what is already there.

It is a **rename**, not a copy: both paths are inside the `/media` mount, which is
the same property the swap itself depends on (§14, 2026-09-03), so the move is a
metadata operation no matter how many terabytes are in the directory. If it ever
turns out not to be a rename, this gives up rather than falling back to
copy-and-delete -- unlinking an original is reserved to nobody at all.

Why this is periodic worker work rather than a startup step or an Alembic revision:
`Scheduler.tick()` runs only when `poll_once` found nothing to do, so the task
inherits "the queue is idle" for free, and `recover_stale` is registered ahead of it
so a swap that crashed mid-rename has already been resolved. That ordering is
load-bearing. `swap.recover()` decides what happened by the **size of the files at
the journalled paths**; move a backup out from under an unresolved `swap.plan.json`
and it reads "source gone, backup gone" and concludes `stale`, which is both wrong
and unrecoverable. An Alembic revision would be worse on both counts: it has no
`Settings`, and it would move a library inside a schema upgrade.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vidcleaner.config import Settings, get_settings
from vidcleaner.db.constants import RUNNING_STATES
from vidcleaner.db.models import Backup, Job
from vidcleaner.logging import get_logger
from vidcleaner.pipeline.swap import (
    IGNORE_MARKER,
    README_MARKER,
    FsOps,
    RealFs,
    write_dir_markers,
)

__all__ = ["RelocateReport", "migrate_legacy_backups"]

log = get_logger(__name__)

#: Files this directory holds that are ours rather than a user's original.
_OUR_MARKERS = frozenset({IGNORE_MARKER, README_MARKER})


@dataclass(frozen=True, slots=True)
class RelocateReport:
    moved: int = 0
    moved_bytes: int = 0
    rows_updated: int = 0
    skipped: tuple[str, ...] = field(default_factory=tuple)
    """Files left where they were, with the reason. The legacy directory stays too."""
    legacy_removed: bool = False
    blocked: str | None = None
    """Why nothing ran. `None` means the run happened, even if it moved nothing."""

    @property
    def moved_mib(self) -> float:
        return round(self.moved_bytes / 1_048_576, 1)


def _blocking_reason(session: Session, settings: Settings, legacy: Path) -> str | None:
    """Everything that must be true before a single file moves.

    The idle gate the scheduler provides is about *this* worker; these three are
    about the install. Any of them failing means "not now", never "not ever" -- the
    task simply runs again at the next interval.
    """
    if not legacy.is_dir():
        return "no legacy directory"
    try:
        if legacy.resolve() == settings.backups_dir.resolve():
            return "already the configured backups directory"
    except OSError as exc:  # pragma: no cover - unreadable mount
        return f"cannot resolve {legacy}: {exc}"

    running = session.scalars(select(Job.id).where(Job.state.in_(RUNNING_STATES)).limit(1)).first()
    if running is not None:
        # Our own queue is idle or `tick` would not have been reached; this catches a
        # second worker, which the idle gate knows nothing about.
        return f"job {running} is still running"

    # A swap that crashed between its two renames leaves an intent journal naming the
    # *old* backup path. `swap.run` replays it on the next attempt and `recover` reads
    # file sizes at exactly those paths, so moving a backup out from under one makes it
    # read "source gone, backup gone" and conclude `stale`. Walk `/work` rather than the
    # job table: a `vidcleaner clean --in-place` run writes the same journal with no row
    # behind it at all. `swap.json` is written for every settled outcome -- a successful
    # execute and a recovery that committed -- so its absence is exactly "unresolved".
    if settings.work_dir.is_dir():
        for journal in sorted(settings.work_dir.glob("*/swap.plan.json")):
            if not (journal.parent / "swap.json").is_file():
                return f"{journal} is an unresolved swap journal"
    return None


def migrate_legacy_backups(
    session: Session,
    settings: Settings | None = None,
    *,
    fs: FsOps | None = None,
) -> RelocateReport:
    """Move `<media>/.vidcleaner-backups` into the configured backups directory."""
    settings = settings or get_settings()
    fs = fs or RealFs()
    legacy = settings.legacy_backups_dir
    destination_root = settings.backups_dir

    blocked = _blocking_reason(session, settings, legacy)
    if blocked is not None:
        log.debug("relocate.skipped", reason=blocked)
        return RelocateReport(blocked=blocked)

    moved = rows = 0
    moved_bytes = 0
    skipped: list[str] = []

    for source in sorted(legacy.rglob("*")):
        if not source.is_file():
            continue
        if source.parent == legacy and source.name in _OUR_MARKERS:
            # Our own signposts, not originals. `write_dir_markers` puts fresh ones in
            # the new directory below, and moving these would count them as originals
            # -- or collide with the markers already there and block the cleanup.
            continue
        destination = destination_root / source.relative_to(legacy)
        if fs.exists(destination):
            # Never overwrite an original -- the same rule `plan_swap`'s preflight
            # applies to a backup path that already exists.
            skipped.append(f"{destination} already exists")
            continue
        try:
            size = fs.stat(source).st_size
            fs.mkdirs(destination.parent)
            fs.rename(source, destination)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                message = (
                    f"{legacy} and {destination_root} are on different mounts, so the "
                    "originals cannot be moved without copying and deleting them. Move "
                    "the directory yourself, or point VIDCLEANER_BACKUPS_DIR back at "
                    f"{legacy}."
                )
                log.error("relocate.cross_device", legacy=str(legacy), error=message)
                return RelocateReport(
                    moved=moved,
                    moved_bytes=moved_bytes,
                    rows_updated=rows,
                    skipped=tuple(skipped),
                    blocked=message,
                )
            skipped.append(f"{source}: {exc.strerror or exc}")
            continue

        # Committed per file, not batched: a crash halfway then leaves every file
        # that did move correctly recorded, and the next tick picks up the rest.
        rows += session.execute(
            update(Backup)
            .where(Backup.backup_path == str(source))
            .values(backup_path=str(destination))
        ).rowcount
        session.commit()
        moved += 1
        moved_bytes += size

    write_dir_markers(destination_root, fs)
    removed = _remove_legacy_tree(legacy, fs) if not skipped else False
    if skipped:
        log.warning("relocate.incomplete", legacy=str(legacy), skipped=skipped)
    if moved or removed:
        log.info(
            "relocate.moved",
            legacy=str(legacy),
            destination=str(destination_root),
            files=moved,
            moved_mib=round(moved_bytes / 1_048_576, 1),
            rows=rows,
            legacy_removed=removed,
        )
    return RelocateReport(
        moved=moved,
        moved_bytes=moved_bytes,
        rows_updated=rows,
        skipped=tuple(skipped),
        legacy_removed=removed,
    )


def _remove_legacy_tree(legacy: Path, fs: FsOps) -> bool:
    """Take the empty directory away, markers included, or leave it entirely alone.

    Only reached when every file moved, so the only things that can remain are the
    `.ignore` and `README.txt` we wrote there ourselves.
    """
    try:
        for path in sorted(legacy.rglob("*"), reverse=True):
            if path.is_file():
                if path.parent != legacy or path.name not in _OUR_MARKERS:
                    return False
                fs.unlink(path)
            elif path.is_dir():
                fs.rmdir(path)
        fs.rmdir(legacy)
    except OSError as exc:
        log.warning("relocate.legacy_not_removed", path=str(legacy), error=str(exc))
        return False
    return True
