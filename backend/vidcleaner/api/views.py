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

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.models import Job, MediaItem, MediaItemEpisode, Title

# Re-exported: M5 moved these to `db/queries.py` so the worker can use them without
# importing `vidcleaner.api`. Every M4 import site keeps working.
from vidcleaner.db.queries import EVIDENCE_STATES, evidence_job_ids

__all__ = [
    "EVIDENCE_STATES",
    "ItemRef",
    "JobSummary",
    "TitleRef",
    "episode_spans",
    "evidence_job_ids",
    "episode_code",
    "item_label",
    "item_ref",
    "job_summary",
    "title_ref",
    "utc",
]


def episode_spans(session: Session, items: Sequence[MediaItem]) -> dict[int, list[tuple[int, int]]]:
    """item id -> every (season, episode) it covers. One query, not one per row."""
    ids = [item.id for item in items]
    if not ids:
        return {}
    out: dict[int, list[tuple[int, int]]] = {}
    rows = session.execute(
        select(
            MediaItemEpisode.media_item_id,
            MediaItemEpisode.season,
            MediaItemEpisode.episode,
        )
        .where(MediaItemEpisode.media_item_id.in_(ids))
        .order_by(MediaItemEpisode.season, MediaItemEpisode.episode)
    ).all()
    for item_id, season, episode in rows:
        out.setdefault(item_id, []).append((season, episode))
    return out


def utc(value: datetime | None) -> datetime | None:
    """Tag a naive database timestamp as UTC. Never shifts the instant."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def episode_code(item: MediaItem, spans: Sequence[tuple[int, int]] = ()) -> str:
    """``S01E01``, or ``S01E01-E02`` for a multi-episode file (M5's join table).

    ``spans`` comes from `media_item_episodes`; with none given this falls back to
    the scalar columns, which hold the **lowest** pair. Contiguous runs in one season
    collapse to a range; anything else is listed, because "S01E01-E05" would be a lie
    about a file holding E01 and E05 only.
    """
    if item.season is None or item.episode is None:
        return ""
    pairs = sorted(set(spans)) or [(item.season, item.episode)]
    if len(pairs) == 1:
        return f"S{pairs[0][0]:02d}E{pairs[0][1]:02d}"
    seasons = {s for s, _ in pairs}
    episodes = [e for _, e in pairs]
    contiguous = len(seasons) == 1 and episodes == list(
        range(episodes[0], episodes[0] + len(episodes))
    )
    if contiguous:
        return f"S{pairs[0][0]:02d}E{episodes[0]:02d}-E{episodes[-1]:02d}"
    return "+".join(f"S{s:02d}E{e:02d}" for s, e in pairs)


def item_label(item: MediaItem, title: Title | None, spans: Sequence[tuple[int, int]] = ()) -> str:
    """One line naming the file the way the user thinks of it."""
    name = title.title if title is not None else "Unknown"
    if item.kind == "movie":
        year = f" ({title.year})" if title is not None and title.year else ""
        return f"{name}{year}"
    code = episode_code(item, spans)
    suffix = f" — {item.episode_title}" if item.episode_title else ""
    return f"{name}{' ' + code if code else ''}{suffix}"


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
    episodes: list[list[int]] = Field(default_factory=list)
    """Every ``[season, episode]`` a multi-episode file covers (M5's join table).
    Empty for a movie, and for an episode file covering just its scalar pair."""
    episode_title: str | None = None
    path: str
    size: int | None = None
    duration: float | None = None
    status: str
    last_job_id: str | None = None
    cleaned_at: datetime | None = None


def item_ref(
    item: MediaItem, title: Title | None, spans: Sequence[tuple[int, int]] = ()
) -> ItemRef:
    return ItemRef(
        id=item.id,
        title_id=item.title_id,
        title=title.title if title is not None else "Unknown",
        kind=item.kind,
        label=item_label(item, title, spans),
        episodes=[[s, e] for s, e in sorted(set(spans))] if len(set(spans)) > 1 else [],
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
