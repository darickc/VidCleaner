"""Response shapes shared by the M4 screens (PLAN.md §9).

Three of the four pages show the same two things -- "which library item is this?" and
"what is its job doing?" -- so those live here once rather than being re-derived per
router. Everything is a row-to-model function taking already-loaded ORM objects: the
routers own the queries (and their joins), these own the shape.

**Times leave as UTC with an offset.** The database stores naive UTC (see
``db.session.utcnow``); handing that to a browser unqualified makes every timestamp
wrong by the user's offset, silently, which is exactly the kind of bug a review UI
must not have.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.models import Job, MediaItem, Title

__all__ = [
    "EVIDENCE_STATES",
    "ItemRef",
    "JobSummary",
    "TitleRef",
    "evidence_job_ids",
    "item_label",
    "item_ref",
    "job_summary",
    "title_ref",
    "utc",
]


#: States a job can be in and still have something to say about what is in the file.
#: ``already_clean`` is the interesting exclusion: that job short-circuited at `probe`
#: and never reached `detect`.
EVIDENCE_STATES: tuple[str, ...] = ("done", "failed", "stale")


def evidence_job_ids(session: Session, items: Sequence[MediaItem]) -> dict[int, str]:
    """item id -> the job whose detections describe that file today.

    Usually ``media_items.last_job_id``, which is what §5's rollup query names. But
    the last *run* is not always the last run that **found** anything: the audit pass
    and §4's idempotency check both end ``already_clean`` without reaching `detect`,
    and they do update ``last_job_id`` (they must -- the profile hash they record is
    what stops the hourly sync re-enqueueing the file forever). Reading their empty
    detections would blank the Item page and the Title rollup for a file that is in
    fact full of muted words. Found by the M4 demo, not by review.
    """
    ids = [item.id for item in items]
    if not ids:
        return {}
    rows = session.execute(
        select(Job.id, Job.media_item_id, Job.state)
        .where(Job.media_item_id.in_(ids))
        .order_by(Job.created_at.desc(), Job.id.desc())
    ).all()

    states = {job_id: state for job_id, _, state in rows}
    newest_with_evidence: dict[int, str] = {}
    for job_id, item_id, state in rows:
        if state in EVIDENCE_STATES and item_id not in newest_with_evidence:
            newest_with_evidence[item_id] = job_id

    chosen: dict[int, str] = {}
    for item in items:
        last = item.last_job_id
        if last is not None and states.get(last) not in (None, "already_clean"):
            chosen[item.id] = last
        elif item.id in newest_with_evidence:
            chosen[item.id] = newest_with_evidence[item.id]
        elif last is not None:
            chosen[item.id] = last
    return chosen


def utc(value: datetime | None) -> datetime | None:
    """Tag a naive database timestamp as UTC. Never shifts the instant."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def item_label(item: MediaItem, title: Title | None) -> str:
    """One line naming the file the way the user thinks of it."""
    name = title.title if title is not None else "Unknown"
    if item.kind == "movie":
        year = f" ({title.year})" if title is not None and title.year else ""
        return f"{name}{year}"
    code = ""
    if item.season is not None and item.episode is not None:
        code = f" S{item.season:02d}E{item.episode:02d}"
    suffix = f" — {item.episode_title}" if item.episode_title else ""
    return f"{name}{code}{suffix}"


class TitleRef(BaseModel):
    id: int
    kind: str
    title: str
    year: int | None = None
    poster_url: str | None = None
    enabled: bool = False
    profile_id: int | None = None


def title_ref(title: Title | None) -> TitleRef | None:
    if title is None:
        return None
    return TitleRef(
        id=title.id,
        kind=title.kind,
        title=title.title,
        year=title.year,
        poster_url=title.poster_url,
        enabled=title.enabled,
        profile_id=title.profile_id,
    )


class ItemRef(BaseModel):
    id: int
    title_id: int
    title: str
    kind: str
    label: str
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    path: str
    size: int | None = None
    duration: float | None = None
    status: str
    last_job_id: str | None = None
    cleaned_at: datetime | None = None


def item_ref(item: MediaItem, title: Title | None) -> ItemRef:
    return ItemRef(
        id=item.id,
        title_id=item.title_id,
        title=title.title if title is not None else "Unknown",
        kind=item.kind,
        label=item_label(item, title),
        season=item.season,
        episode=item.episode,
        episode_title=item.episode_title,
        path=item.path,
        size=item.size,
        duration=item.duration,
        status=item.status,
        last_job_id=item.last_job_id,
        cleaned_at=utc(item.cleaned_at),
    )


class JobSummary(BaseModel):
    """One row of the Queue page, and the header of the Item page."""

    id: str
    media_item_id: int
    item: ItemRef | None = None
    trigger: str
    state: str
    stage: str | None = None
    progress_pct: float = 0.0
    priority: int = 100
    attempts: int = 0
    dry_run: bool = False
    force: bool = False
    stt_mode: str | None = None
    model_used: str | None = None
    subtitle_source: str | None = None
    claimed_by: str | None = None
    error: str | None = None
    detections: int | None = None
    """Filled in only where the router counted them; ``None`` means "not asked"."""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat: datetime | None = None
    retry_at: datetime | None = None


def job_summary(
    job: Job,
    item: MediaItem | None = None,
    title: Title | None = None,
    *,
    detections: int | None = None,
) -> JobSummary:
    return JobSummary(
        id=job.id,
        media_item_id=job.media_item_id,
        item=item_ref(item, title) if item is not None else None,
        trigger=job.trigger,
        state=job.state,
        stage=job.stage,
        progress_pct=job.progress_pct,
        priority=job.priority,
        attempts=job.attempts,
        dry_run=job.dry_run,
        force=job.force,
        stt_mode=job.stt_mode,
        model_used=job.model_used,
        subtitle_source=job.subtitle_source,
        claimed_by=job.claimed_by,
        error=job.error,
        detections=detections,
        created_at=utc(job.created_at),
        started_at=utc(job.started_at),
        finished_at=utc(job.finished_at),
        heartbeat=utc(job.heartbeat),
        retry_at=utc(job.retry_at),
    )
