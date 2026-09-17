"""§9.6's "backup retention + purge", and §13's "purge UI".

Retention had a *number* in Settings from M0 and no consumer until M5. This is the
other half: what is being held, how much of the media share it costs, and a way to
reclaim it now rather than waiting for the scheduler.

The summary answers "how much is this costing me, and what can go?". §9.7's Backups
page answers the other half -- *which* originals, and where they came from -- because
an orphan that is only a count cannot be checked before it is deleted, and "the share
is filling up" is really the question "what is the biggest thing in here?".

Still not paginated: a few thousand rows sort and filter fine in one response, and
`limit` caps it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
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
    rel_path: str = ""
    """`backup_path` under `backups_dir`. What the page shows, because the backups tree
    mirrors the library tree -- so this reads `Movies/Foo (2019)/Foo.mkv` even for an
    adopted orphan, whose `original_path` is empty and whose item is a sentinel."""
    identified: bool = True
    """False for a row hung off the `<orphaned backups>` sentinel: there is no item
    page to link to, and nothing to restore it into."""
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
    backups_dir_is_hidden: bool = False
    """The old default was dot-prefixed, and an installed container keeps the variable
    it was created with. The page says so rather than leaving "where are my backups?"
    to be answered by a file browser that does not show them."""


class BackupList(BaseModel):
    summary: BackupSummary
    backups: list[BackupRow] = Field(default_factory=list)


class PurgeRequest(BaseModel):
    scope: Literal["expired", "orphaned"] = "expired"


class ReconcileResult(BaseModel):
    adopted: int = 0
    purged: int = 0
    skipped: bool = False
    note: str = ""


class PurgeResult(BaseModel):
    scope: str
    purged: int = 0
    freed_bytes: int = 0
    missing: int = 0
    warnings: list[str] = Field(default_factory=list)


def _relative(path: str, root: Path) -> str:
    """The backups tree mirrors the library tree, so this is the readable name."""
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        # A row from an older `backups_dir`, or one pointing somewhere it should not.
        # `purge_backups` refuses those; showing the full path is how they get noticed.
        return path


def _sentinel_item_id(db: Session) -> int | None:
    """The `<orphaned backups>` item adopted files hang from (`persist.ORPHAN_PATH`).

    `backups.media_item_id` is NOT NULL, so a file whose row was lost to a crash has
    to belong to *something*; it is not an episode, and the page must not offer a link
    or a restore for it.
    """
    from vidcleaner.pipeline.persist import ORPHAN_PATH  # noqa: PLC0415

    return db.scalars(select(MediaItem.id).where(MediaItem.path == ORPHAN_PATH)).first()


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
    sort: Annotated[Literal["recent", "largest"], Query()] = "recent",
    expired_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> BackupList:
    """What `/backups` is holding, with the totals the Settings page shows."""
    settings = load_settings(db)
    deploy = get_settings()
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
        backups_dir=str(deploy.backups_dir),
        backups_dir_is_hidden=deploy.backups_dir_is_hidden,
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

    # "Largest first" is the question behind "the share is filling up"; "newest first"
    # is the one behind "what did that last job keep?".
    order = (
        (Backup.size.desc().nullslast(), Backup.id.desc())
        if sort == "largest"
        else (Backup.created_at.desc(), Backup.id.desc())
    )
    query = select(Backup).order_by(*order).limit(limit)
    if state:
        query = query.where(Backup.state == state)
    if expired_only:
        query = query.where(Backup.purge_after.is_not(None), Backup.purge_after <= now)
    rows = list(db.scalars(query).all())
    labels = _labels(db, rows)
    sentinel = _sentinel_item_id(db)

    return BackupList(
        summary=summary,
        backups=[
            BackupRow(
                id=row.id,
                media_item_id=row.media_item_id,
                label=labels.get(row.media_item_id, f"item {row.media_item_id}"),
                original_path=row.original_path,
                backup_path=row.backup_path,
                rel_path=_relative(row.backup_path, deploy.backups_dir),
                identified=row.media_item_id != sentinel,
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


@router.post("/backups/reconcile", response_model=ReconcileResult)
def reconcile(db: DbSession) -> ReconcileResult:
    """Re-read the directory before showing it.

    The scheduler reconciles hourly, and §9.7's page exists precisely so orphans can
    be looked at before they are deleted -- a list that is up to an hour stale is the
    wrong thing to put a purge button next to.
    """
    from vidcleaner.pipeline.persist import reconcile_backups  # noqa: PLC0415

    deploy = get_settings()
    report = reconcile_backups(
        db,
        deploy.backups_dir,
        retention_days=load_settings(db).backup_retention_days,
        legacy_dir=deploy.legacy_backups_dir,
    )
    if report.skipped:
        return ReconcileResult(
            skipped=True,
            note=(
                f"Originals are still being moved out of {deploy.legacy_backups_dir}. "
                "This will run by itself once that finishes."
            ),
        )
    log.info("backups.reconciled_by_user", adopted=report.adopted, purged=report.purged)
    return ReconcileResult(adopted=report.adopted, purged=report.purged)


@router.delete("/backups/{backup_id}", response_model=PurgeResult)
def purge_one(backup_id: int, db: DbSession) -> PurgeResult:
    """Delete one named original, so a single huge orphan can go on its own.

    Routed through the same `purge_backups` as both bulk buttons, with `ids` selecting
    rather than a second deletion path: every refusal -- outside `backups_dir`, a
    `restored` row, a file already gone -- has to apply here unchanged.
    """
    from vidcleaner.worker.purge import purge_backups  # noqa: PLC0415

    row = db.get(Backup, backup_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such backup")

    report = purge_backups(db, get_settings(), ids=[backup_id])
    if not (report.purged or report.missing or report.refused):
        # Selected but not purgeable: a `restored` row, whose file went back into the
        # library, or an already-`purged` one.
        raise HTTPException(status_code=409, detail=f"a {row.state} backup cannot be purged")
    log.info("backups.purged_one_by_user", backup_id=backup_id, freed_mib=report.freed_mib)
    return PurgeResult(
        scope="one",
        purged=report.purged,
        freed_bytes=report.freed_bytes,
        missing=report.missing,
        warnings=list(report.refused),
    )
