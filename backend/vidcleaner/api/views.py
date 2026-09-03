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

from datetime import UTC, datetime

from pydantic import BaseModel

from vidcleaner.db.models import Job, MediaItem, Title

__all__ = [
    "ItemRef",
    "JobSummary",
    "TitleRef",
    "item_label",
    "item_ref",
    "job_summary",
    "title_ref",
    "utc",
]


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
