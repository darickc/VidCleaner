"""§9.6's "backup retention + purge", and §13's "purge UI".

Retention had a *number* in Settings from M0 and no consumer until M5. This is the
other half: what is being held, how much of the media share it costs, and a way to
reclaim it now rather than waiting for the scheduler.

The list is deliberately not paginated per item: a library of a few thousand episodes
has a few thousand rows, the page shows totals plus the oldest, and the useful question
("how much is this costing me, and what can go?") is answered by the summary rather
than by scrolling.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vidcleaner.api.views import item_label, utc
from vidcleaner.config import get_settings
from vidcleaner.db.models import Backup, MediaItem, Title
from vidcleaner.db.session import get_db, utcnow
from vidcleaner.logging import get_logger
from vidcleaner.settings_store import load_settings

router = APIRouter(tags=["backups"])
DbSession = Annotated[Session, Depends(get_db)]
log = get_logger(__name__)


class BackupRow(BaseModel):
    id: int
    media_item_id: int
    label: str
    original_path: str
    backup_path: str
    size: int | None = None
    state: str
    exists: bool = True
    """False once the file is gone but the row has not been reconciled yet."""
    created_at: datetime | None = None
    purge_after: datetime | None = None
    expired: bool = False


class BackupSummary(BaseModel):
    total: int = 0
    total_bytes: int = 0
    by_state: dict[str, int] = Field(default_factory=dict)
    bytes_by_state: dict[str, int] = Field(default_factory=dict)
    expired: int = 0
    expired_bytes: int = 0
    """What "purge now" would reclaim -- the number worth putting on a button."""
    orphaned: int = 0
    orphaned_bytes: int = 0
    retention_days: int = 30
    keeps_forever: bool = False
    """``backup_retention_days == 0``: `purge_after` is NULL and nothing expires."""
    backups_dir: str = ""


class BackupList(BaseModel):
    summary: BackupSummary
    backups: list[BackupRow] = Field(default_factory=list)


class PurgeRequest(BaseModel):
    scope: Literal["expired", "orphaned"] = "expired"


class PurgeResult(BaseModel):
    scope: str
    purged: int = 0
    freed_bytes: int = 0
    missing: int = 0
    warnings: list[str] = Field(default_factory=list)


def _labels(db: Session, rows: list[Backup]) -> dict[int, str]:
    ids = {r.media_item_id for r in rows}
    if not ids:
        return {}
    out: dict[int, str] = {}
    for item in db.scalars(select(MediaItem).where(MediaItem.id.in_(ids))):
        out[item.id] = item_label(item, db.get(Title, item.title_id))
    return out


@router.get("/backups", response_model=BackupList)
def list_backups(
    db: DbSession,
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> BackupList:
    """What `/backups` is holding, with the totals the Settings page shows."""
    settings = load_settings(db)
    now = utcnow()

    counts = db.execute(
        select(Backup.state, func.count(), func.coalesce(func.sum(Backup.size), 0)).group_by(
            Backup.state
        )
    ).all()
    summary = BackupSummary(
        total=sum(c for _, c, _ in counts),
        total_bytes=sum(int(b) for _, _, b in counts),
        by_state={s: c for s, c, _ in counts},
        bytes_by_state={s: int(b) for s, _, b in counts},
        retention_days=settings.backup_retention_days,
        keeps_forever=settings.backup_retention_days == 0,
        backups_dir=str(get_settings().backups_dir),
    )

    expired = db.execute(
        select(func.count(), func.coalesce(func.sum(Backup.size), 0)).where(
            Backup.state.in_(("kept", "orphaned")),
            Backup.purge_after.is_not(None),
            Backup.purge_after <= now,
        )
    ).one()
    summary.expired, summary.expired_bytes = int(expired[0]), int(expired[1])

    orphaned = db.execute(
        select(func.count(), func.coalesce(func.sum(Backup.size), 0)).where(
            Backup.state == "orphaned"
        )
    ).one()
    summary.orphaned, summary.orphaned_bytes = int(orphaned[0]), int(orphaned[1])

    query = select(Backup).order_by(Backup.created_at.desc(), Backup.id.desc()).limit(limit)
    if state:
        query = query.where(Backup.state == state)
    rows = list(db.scalars(query).all())
    labels = _labels(db, rows)

    return BackupList(
        summary=summary,
        backups=[
            BackupRow(
                id=row.id,
                media_item_id=row.media_item_id,
                label=labels.get(row.media_item_id, f"item {row.media_item_id}"),
                original_path=row.original_path,
                backup_path=row.backup_path,
                size=row.size,
                state=row.state,
                exists=Path(row.backup_path).is_file(),
                created_at=utc(row.created_at),
                purge_after=utc(row.purge_after),
                expired=bool(row.purge_after and row.purge_after <= now),
            )
            for row in rows
        ],
    )


@router.post("/backups/purge", response_model=PurgeResult)
def purge(request: Annotated[PurgeRequest, Body()], db: DbSession) -> PurgeResult:
    """§13's purge button. Irreversible, which is why the UI puts it behind a confirm.

    Runs the same `purge_backups` the scheduler does, so there is one definition of
    what may be deleted -- including the guard that refuses any path outside
    ``backups_dir``.
    """
    from vidcleaner.worker.purge import purge_backups  # noqa: PLC0415

    report = purge_backups(db, get_settings(), scope=request.scope)
    log.info(
        "backups.purged_by_user",
        scope=request.scope,
        purged=report.purged,
        freed_mib=report.freed_mib,
    )
    return PurgeResult(
        scope=request.scope,
        purged=report.purged,
        freed_bytes=report.freed_bytes,
        missing=report.missing,
        warnings=list(report.refused),
    )
