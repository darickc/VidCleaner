"""The Queue page's API (PLAN.md §9.1) — reads here, actions in :mod:`api.actions`.

``GET /api/jobs`` answers the whole screen in one request: what is running, what is
waiting, and what recently finished. The page polls it every couple of seconds, so it
is deliberately three bounded queries rather than one unbounded list the client has to
bucket itself — and the running job is never truncated away by a page of queued work.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from vidcleaner.api.views import JobSummary, job_summary, utc
from vidcleaner.db.constants import RUNNING_STATES, TERMINAL_STATES
from vidcleaner.db.models import Detection, Job, JobLog, MediaItem, Title
from vidcleaner.db.session import get_db

router = APIRouter(tags=["jobs"])
DbSession = Annotated[Session, Depends(get_db)]

#: How many finished jobs the Queue page shows without asking for more.
RECENT_DEFAULT = 20
MAX_LIMIT = 200


class QueueView(BaseModel):
    running: list[JobSummary]
    queued: list[JobSummary]
    recent: list[JobSummary]
    queued_total: int
    """Rows beyond ``queued``'s limit still exist; the page says how many."""


class JobLogLine(BaseModel):
    id: int
    ts: datetime | None = None
    level: str
    msg: str


class JobDetail(JobSummary):
    work_dir: str | None = None
    source_fingerprint: str | None = None
    timings: dict[str, float] = Field(default_factory=dict)
    profile: dict = Field(default_factory=dict)
    logs: list[JobLogLine] = Field(default_factory=list)


def _rows(db: Session, *conditions, order, limit: int) -> list[JobSummary]:
    """Jobs with their item and title in one query -- the Queue page is a list of
    names, and a per-row lazy load would be N+1 on every poll."""
    stmt = (
        select(Job, MediaItem, Title)
        .join(MediaItem, MediaItem.id == Job.media_item_id)
        .join(Title, Title.id == MediaItem.title_id, isouter=True)
        .order_by(*order)
        .limit(limit)
    )
    for condition in conditions:
        stmt = stmt.where(condition)
    return [job_summary(job, item, title) for job, item, title in db.execute(stmt).all()]


@router.get("/jobs", response_model=QueueView)
def read_queue(
    db: DbSession,
    queued_limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    recent_limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = RECENT_DEFAULT,
) -> QueueView:
    running = _rows(
        db,
        Job.state.in_(RUNNING_STATES),
        order=(Job.started_at.desc().nulls_last(), Job.created_at),
        limit=MAX_LIMIT,
    )
    queued = _rows(
        db,
        Job.state == "queued",
        # The claim order, exactly: `lower priority runs sooner`, then age.
        order=(Job.priority, Job.created_at),
        limit=queued_limit,
    )
    recent = _rows(
        db,
        Job.state.in_(TERMINAL_STATES),
        order=(Job.finished_at.desc().nulls_last(), Job.created_at.desc()),
        limit=recent_limit,
    )
    queued_total = (
        db.scalar(select(func.count()).select_from(Job).where(Job.state == "queued")) or 0
    )
    return QueueView(
        running=running,
        queued=queued,
        recent=recent,
        queued_total=queued_total,
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
def read_job(
    job_id: str, db: DbSession, log_limit: Annotated[int, Query(ge=0, le=500)] = 200
) -> JobDetail:
    row = db.execute(
        select(Job, MediaItem, Title)
        .join(MediaItem, MediaItem.id == Job.media_item_id)
        .join(Title, Title.id == MediaItem.title_id, isouter=True)
        .where(Job.id == job_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    job, item, title = row

    detections = (
        db.scalar(select(func.count()).select_from(Detection).where(Detection.job_id == job.id))
        or 0
    )
    summary = job_summary(job, item, title, detections=detections)

    def parse(raw: str | None) -> dict:
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    logs = db.scalars(
        select(JobLog).where(JobLog.job_id == job.id).order_by(JobLog.ts, JobLog.id)
    ).all()
    # The tail, in order: a long-running job's interesting lines are the last ones.
    tail = logs[-log_limit:] if log_limit else []

    return JobDetail(
        **summary.model_dump(),
        work_dir=job.work_dir,
        source_fingerprint=job.source_fingerprint,
        timings={k: float(v) for k, v in parse(job.timings_json).items()},
        profile=parse(job.profile_snapshot_json),
        logs=[
            JobLogLine(id=line.id, ts=utc(line.ts), level=line.level, msg=line.msg) for line in tail
        ],
    )
