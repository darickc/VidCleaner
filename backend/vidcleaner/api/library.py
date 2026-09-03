"""The Library and Title pages (PLAN.md §9.2 and §9.3).

The Library page is a list of titles with a "12/24 clean" progress figure, and the
Title page is one title's items plus its per-word rollup. Both counts are computed in
SQL over ``media_items.status`` and ``detections`` rather than by loading rows: a
library is thousands of episodes and this endpoint is polled while a backfill runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from vidcleaner.api.views import ItemRef, TitleRef, item_ref, title_ref, utc
from vidcleaner.db.models import Detection, MediaItem, Profile, Title
from vidcleaner.db.session import get_db

router = APIRouter(prefix="/library", tags=["library"])
DbSession = Annotated[Session, Depends(get_db)]

#: Statuses that count towards "done" in a title's progress figure. ``already_clean``
#: is included on purpose: nothing more will happen to that file, which is what the
#: number is telling the user.
DONE_STATUSES = ("clean", "already_clean")


class TitleRow(TitleRef):
    arr_id: int
    arr_path: str | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    item_count: int = 0
    clean_count: int = 0
    failed_count: int = 0
    pending_count: int = 0
    last_synced_at: datetime | None = None


class TitleList(BaseModel):
    titles: list[TitleRow]
    total: int
    """Matching rows, before ``limit`` -- so the page can say "showing 50 of 812"."""


class WordCountRow(BaseModel):
    """§5's rollup query, as a row: ``word, category, count, muted``."""

    word_canonical: str
    category: str
    total: int
    muted: int


class ItemRow(ItemRef):
    detection_count: int = 0
    """From the item's last job, whitelisted hits excluded -- the number the Title
    page shows next to each episode."""


class TitleDetail(BaseModel):
    title: TitleRow
    profile_name: str | None = None
    items: list[ItemRow] = Field(default_factory=list)
    counts: list[WordCountRow] = Field(default_factory=list)


def _status_counts():
    """One aggregate per status bucket, grouped by title."""
    return (
        func.count(MediaItem.id).label("item_count"),
        func.sum(case((MediaItem.status.in_(DONE_STATUSES), 1), else_=0)).label("clean_count"),
        func.sum(case((MediaItem.status == "failed", 1), else_=0)).label("failed_count"),
        func.sum(case((MediaItem.status.notin_((*DONE_STATUSES, "failed")), 1), else_=0)).label(
            "pending_count"
        ),
    )


def _title_row(title: Title, counts: tuple[int | None, ...]) -> TitleRow:
    base = title_ref(title)
    assert base is not None
    item_count, clean_count, failed_count, pending_count = counts
    return TitleRow(
        **base.model_dump(),
        arr_id=title.arr_id,
        arr_path=title.arr_path,
        tvdb_id=title.tvdb_id,
        tmdb_id=title.tmdb_id,
        item_count=item_count or 0,
        clean_count=clean_count or 0,
        failed_count=failed_count or 0,
        pending_count=pending_count or 0,
        last_synced_at=utc(title.last_synced_at),
    )


@router.get("/titles", response_model=TitleList)
def read_titles(
    db: DbSession,
    kind: Literal["series", "movie"] | None = None,
    enabled: bool | None = None,
    q: Annotated[str, Query(max_length=200)] = "",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TitleList:
    stmt = (
        select(Title, *_status_counts())
        .join(MediaItem, MediaItem.title_id == Title.id, isouter=True)
        .group_by(Title.id)
        .order_by(func.lower(Title.title), Title.year)
    )
    count_stmt = select(func.count()).select_from(Title)
    for condition in (
        (Title.kind == kind) if kind else None,
        (Title.enabled.is_(enabled)) if enabled is not None else None,
        Title.title.ilike(f"%{q}%") if q else None,
    ):
        if condition is not None:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

    rows = db.execute(stmt.limit(limit).offset(offset)).all()
    return TitleList(
        titles=[_title_row(row[0], tuple(row[1:])) for row in rows],
        total=db.scalar(count_stmt) or 0,
    )


@router.get("/titles/{title_id}", response_model=TitleDetail)
def read_title(title_id: int, db: DbSession) -> TitleDetail:
    row = db.execute(
        select(Title, *_status_counts())
        .join(MediaItem, MediaItem.title_id == Title.id, isouter=True)
        .where(Title.id == title_id)
        .group_by(Title.id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no title {title_id}")
    title = row[0]

    items = db.scalars(
        select(MediaItem)
        .where(MediaItem.title_id == title_id)
        # Movies have no season/episode; the nulls sort together and the path breaks
        # the tie, so a multi-file movie folder still lists deterministically.
        .order_by(MediaItem.season, MediaItem.episode, MediaItem.path)
    ).all()

    # Detections of each item's *last* job only. A reprocess leaves the old job's rows
    # in place (they are the record of what that run did), so counting them all would
    # double every reprocessed episode.
    last_jobs = {i.last_job_id for i in items if i.last_job_id}
    per_item: dict[int, int] = {}
    if last_jobs:
        for item_id, count in db.execute(
            select(Detection.media_item_id, func.count())
            .where(Detection.job_id.in_(last_jobs), Detection.whitelisted.is_(False))
            .group_by(Detection.media_item_id)
        ).all():
            per_item[item_id] = count

    counts = [
        WordCountRow(word_canonical=word, category=category, total=total, muted=muted or 0)
        for word, category, total, muted in db.execute(
            select(
                Detection.word_canonical,
                Detection.category,
                func.count(),
                func.sum(case((Detection.muted.is_(True), 1), else_=0)),
            )
            .where(
                Detection.job_id.in_(last_jobs or {""}),
                Detection.whitelisted.is_(False),
            )
            .group_by(Detection.word_canonical, Detection.category)
            .order_by(func.count().desc(), Detection.word_canonical)
        ).all()
    ]

    profile = db.get(Profile, title.profile_id) if title.profile_id else None
    return TitleDetail(
        title=_title_row(title, tuple(row[1:])),
        profile_name=profile.name if profile else None,
        items=[
            ItemRow(**item_ref(item, title).model_dump(), detection_count=per_item.get(item.id, 0))
            for item in items
        ],
        counts=counts,
    )
