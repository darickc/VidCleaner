"""The Item page (PLAN.md §9.4): what was removed from one file, and the evidence.

Everything the page shows comes from one request: the file, the job that cleaned it,
the per-word counts, every detection with the paths of its review clips, the whitelist
entries currently in scope, and whether a backup exists to restore. Detections belong
to a *job*, not to the item -- a reprocess writes a second set -- so the default is the
item's last job and ``?job_id=`` shows an earlier run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from vidcleaner.api.views import (
    ItemRef,
    JobSummary,
    TitleRef,
    item_ref,
    job_summary,
    title_ref,
    utc,
)
from vidcleaner.config import get_settings
from vidcleaner.db.models import Backup, Detection, Job, MediaItem, Title, WhitelistEntry
from vidcleaner.db.session import get_db

router = APIRouter(tags=["items"])
DbSession = Annotated[Session, Depends(get_db)]


class DetectionRow(BaseModel):
    id: int
    word_raw: str
    word_canonical: str
    category: str
    start_s: float
    end_s: float
    mute_start_s: float
    mute_end_s: float
    source: str
    confidence: float | None = None
    muted: bool
    whitelisted: bool
    suspicious: bool
    subtitle_cue_idx: int | None = None
    snippet: str | None = None
    """URL prefix of this detection's clips, or ``None`` when they were never made
    (a dry run, a pruned resume) or have since been deleted. The page hides its
    players rather than offering a broken one."""


class WordCountRow(BaseModel):
    word_canonical: str
    category: str
    total: int
    muted: int
    suspicious: int


class WhitelistRow(BaseModel):
    id: int
    scope: str
    scope_id: int | None = None
    canonical_word: str
    context_text: str | None = None


class BackupRow(BaseModel):
    id: int
    backup_path: str
    original_path: str
    size: int | None = None
    state: str
    created_at: datetime | None = None
    purge_after: datetime | None = None


class ItemDetail(BaseModel):
    item: ItemRef
    title: TitleRef | None = None
    job: JobSummary | None = None
    jobs: list[JobSummary] = Field(default_factory=list)
    """Every run for this file, newest first, so the page can switch between them."""
    counts: list[WordCountRow] = Field(default_factory=list)
    detections: list[DetectionRow] = Field(default_factory=list)
    whitelist: list[WhitelistRow] = Field(default_factory=list)
    backups: list[BackupRow] = Field(default_factory=list)
    restorable: bool = False


def snippet_url(snippet_path: str | None) -> str | None:
    """``<job id>/<nnnn>`` -> the media route, but only if the files are still there.

    Checking existence costs one ``stat`` per detection and buys a page that never
    shows a play button that 404s -- clips can be deleted by retention or by a user
    clearing ``/config``."""
    if not snippet_path:
        return None
    directory = get_settings().snippets_dir / snippet_path
    if not (directory / "orig.m4a").is_file():
        return None
    return f"/api/media/snippets/{snippet_path}"


@router.get("/items/{item_id}", response_model=ItemDetail)
def read_item(item_id: int, db: DbSession, job_id: str | None = None) -> ItemDetail:
    item = db.get(MediaItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no media item {item_id}")
    title = db.get(Title, item.title_id)

    runs = db.scalars(
        select(Job).where(Job.media_item_id == item_id).order_by(Job.created_at.desc(), Job.id)
    ).all()
    chosen = None
    wanted = job_id or item.last_job_id
    if wanted is not None:
        chosen = next((j for j in runs if j.id == wanted), None)
    if chosen is None and job_id is not None:
        raise HTTPException(status_code=404, detail=f"job {job_id} is not a run of this item")
    if chosen is None:
        # No `last_job_id` yet (or it points at a pruned row): show the newest run.
        chosen = runs[0] if runs else None

    detections: list[Detection] = []
    counts: list[WordCountRow] = []
    if chosen is not None:
        detections = list(
            db.scalars(
                select(Detection)
                .where(Detection.job_id == chosen.id)
                .order_by(Detection.start_s, Detection.id)
            ).all()
        )
        counts = [
            WordCountRow(
                word_canonical=word,
                category=category,
                total=total,
                muted=muted or 0,
                suspicious=suspicious or 0,
            )
            for word, category, total, muted, suspicious in db.execute(
                select(
                    Detection.word_canonical,
                    Detection.category,
                    func.count(),
                    func.sum(case((Detection.muted.is_(True), 1), else_=0)),
                    func.sum(case((Detection.suspicious.is_(True), 1), else_=0)),
                )
                .where(Detection.job_id == chosen.id, Detection.whitelisted.is_(False))
                .group_by(Detection.word_canonical, Detection.category)
                .order_by(func.count().desc(), Detection.word_canonical)
            ).all()
        ]

    whitelist = db.scalars(
        select(WhitelistEntry)
        .where(
            (WhitelistEntry.scope == "global")
            | ((WhitelistEntry.scope == "title") & (WhitelistEntry.scope_id == item.title_id))
            | ((WhitelistEntry.scope == "item") & (WhitelistEntry.scope_id == item_id))
        )
        .order_by(WhitelistEntry.canonical_word, WhitelistEntry.id)
    ).all()

    backups = db.scalars(
        select(Backup)
        .where(Backup.media_item_id == item_id)
        .order_by(Backup.created_at.desc(), Backup.id.desc())
    ).all()

    return ItemDetail(
        item=item_ref(item, title),
        title=title_ref(title),
        job=job_summary(chosen, item, title, detections=len(detections)) if chosen else None,
        jobs=[job_summary(run) for run in runs],
        counts=counts,
        detections=[
            DetectionRow(
                id=d.id,
                word_raw=d.word_raw,
                word_canonical=d.word_canonical,
                category=d.category,
                start_s=d.start_s,
                end_s=d.end_s,
                mute_start_s=d.mute_start_s,
                mute_end_s=d.mute_end_s,
                source=d.source,
                confidence=d.confidence,
                muted=d.muted,
                whitelisted=d.whitelisted,
                suspicious=d.suspicious,
                subtitle_cue_idx=d.subtitle_cue_idx,
                snippet=snippet_url(d.snippet_path),
            )
            for d in detections
        ],
        whitelist=[
            WhitelistRow(
                id=w.id,
                scope=w.scope,
                scope_id=w.scope_id,
                canonical_word=w.canonical_word,
                context_text=w.context_text,
            )
            for w in whitelist
        ],
        backups=[
            BackupRow(
                id=b.id,
                backup_path=b.backup_path,
                original_path=b.original_path,
                size=b.size,
                state=b.state,
                created_at=utc(b.created_at),
                purge_after=utc(b.purge_after),
            )
            for b in backups
        ],
        restorable=any(b.state == "kept" for b in backups),
    )
