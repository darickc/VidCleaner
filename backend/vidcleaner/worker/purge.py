"""Backup retention (§13's "Retention (default 30 days), purge UI").

Until M5, ``backups.purge_after`` had three writers and **no readers**: the column was
filled in on every swap, upgrade and reconcile, the number was editable in §9.6's
Settings form, and nothing ever deleted anything. §13 lists "backups double storage" as
a risk and names retention as its mitigation, so this is that mitigation.

This is **the only code in the project that deletes a file the user might still want**,
and the originals it removes are the whole basis of "every change is reversible". So it
is deliberately narrow: it acts on rows, never on a directory walk; it refuses any path
that is not inside ``backups_dir``; and it will not take the last restorable original
out from under a file that is still cleaned unless that original's own clock has
actually run out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.config import Settings, get_settings
from vidcleaner.db.models import Backup, MediaItem
from vidcleaner.db.session import session_scope, utcnow
from vidcleaner.logging import get_logger

__all__ = ["PurgeReport", "purge_backups", "purge_now"]

log = get_logger(__name__)

#: States whose file is still on disk and therefore purgeable. `restored` is excluded
#: because that file went back into the library; `purged` is already done.
PURGEABLE_STATES = ("kept", "orphaned")


@dataclass(frozen=True, slots=True)
class PurgeReport:
    purged: int = 0
    freed_bytes: int = 0
    missing: int = 0
    """Rows whose file was already gone. Marked `purged` and not an error."""
    refused: tuple[str, ...] = field(default_factory=tuple)
    """Paths left alone, with the reason -- surfaced so a misconfiguration is visible
    rather than silently meaning "nothing to purge"."""

    @property
    def freed_mib(self) -> float:
        return round(self.freed_bytes / 1_048_576, 1)


def _inside(path: Path, root: Path) -> bool:
    """Is ``path`` genuinely under ``root``, following symlinks?

    The same guard `api/media.py` applies to snippet paths, and for the same reason: a
    row is data, and a `backup_path` pointing at the library (or anywhere else) must
    cost nothing. `resolve()` is what makes a planted symlink fail this.
    """
    try:
        resolved = path.resolve()
        return resolved == root.resolve() or root.resolve() in resolved.parents
    except OSError:  # pragma: no cover - unreadable mount
        return False


def _last_restorable(session: Session, backup: Backup) -> bool:
    """Is this the only `kept` original standing between a cleaned file and no undo?"""
    if backup.state != "kept":
        return False
    item = session.get(MediaItem, backup.media_item_id)
    if item is None or item.status != "clean":
        return False
    others = session.scalars(
        select(Backup.id).where(
            Backup.media_item_id == backup.media_item_id,
            Backup.state == "kept",
            Backup.id != backup.id,
            Backup.backup_path.notlike("%.srt"),
        )
    ).all()
    return not others


def purge_backups(
    session: Session,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    scope: str = "expired",
) -> PurgeReport:
    """Delete backups whose retention has run out. Returns what it did.

    ``scope="expired"`` is the scheduled behaviour: rows with a ``purge_after`` in the
    past. ``purge_after`` is NULL when ``backup_retention_days`` is 0, and a NULL is
    **never** purged -- that is how "keep forever" is expressed, and it must not be
    reinterpretable as "expired long ago".

    ``scope="orphaned"`` is §9.6's manual button for the storage an upgrade or a delete
    left behind: rows already marked `orphaned`, regardless of their clock, because the
    file they backed up is not in the library any more and nothing will ever restore it.
    """
    settings = settings or get_settings()
    now = now or utcnow()
    root = settings.backups_dir

    if scope == "orphaned":
        rows = session.scalars(select(Backup).where(Backup.state == "orphaned")).all()
    else:
        rows = session.scalars(
            select(Backup).where(
                Backup.state.in_(PURGEABLE_STATES),
                Backup.purge_after.is_not(None),
                Backup.purge_after <= now,
            )
        ).all()

    purged = missing = freed = 0
    refused: list[str] = []
    for row in rows:
        path = Path(row.backup_path)
        if not _inside(path, root):
            refused.append(f"{row.backup_path}: outside {root}")
            log.warning("purge.refused", backup_id=row.id, path=row.backup_path)
            continue
        if _last_restorable(session, row):
            # Its own clock has run out, so this is not a veto -- but it is worth
            # saying out loud, because it is the moment "restore original" stops
            # being possible for that episode.
            log.info("purge.last_original", backup_id=row.id, media_item_id=row.media_item_id)
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        try:
            if path.is_file():
                path.unlink()
                purged += 1
                freed += size
            else:
                # `reconcile_backups` already models this: a human deleted it, or a
                # previous run got as far as the unlink and not the commit.
                missing += 1
        except OSError as exc:
            refused.append(f"{row.backup_path}: {exc}")
            log.warning("purge.failed", backup_id=row.id, error=str(exc)[:200])
            continue
        row.state = "purged"
    session.flush()

    report = PurgeReport(purged=purged, freed_bytes=freed, missing=missing, refused=tuple(refused))
    if purged or missing or refused:
        log.info(
            "purge.done",
            scope=scope,
            purged=report.purged,
            freed_mib=report.freed_mib,
            missing=report.missing,
            refused=len(report.refused),
        )
    return report


def purge_now(settings: Settings | None = None, *, scope: str = "expired") -> PurgeReport:
    """§9.6's button, and the scheduler's task. Opens its own short transaction."""
    with session_scope() as session:
        return purge_backups(session, settings, scope=scope)
