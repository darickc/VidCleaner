"""Turning a claimed `jobs` row into a `JobSpec` and a work dir.

This is where the database and the pipeline meet, and it is deliberately the *only*
such place on the worker path: everything downstream reads ``job.json`` and the other
artifacts, per CLAUDE.md's "pure function of its on-disk inputs".

The subtle part is resume. The CLI gets safety for free, because
``deterministic_job_id`` folds the profile hash and a non-default ``stt_mode`` into the
work directory's *name* -- change either and you get a different directory, so you
cannot resume onto artifacts computed under different rules. Worker jobs use the
``jobs`` uuid instead, so that protection is gone, and ``build_context``
unconditionally overwrites ``job.json``: a changed profile would silently replace
artifact zero while the old stage markers survived, and ``transcribe``/``detect`` would
be skipped on a transcript computed for a different word list. That is exactly the bug
M2 step 1 found for ``--stt-mode``, arriving by a different route. :func:`plan_job`
therefore compares the on-disk spec against the fresh one and clears the markers when
they disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from vidcleaner.config import Settings, get_settings
from vidcleaner.db.models import Job, MediaItem, Title
from vidcleaner.logging import get_logger
from vidcleaner.matching.compiler import Matcher
from vidcleaner.matching.profile import matcher_for, snapshot_for
from vidcleaner.pipeline.artifacts import ArtifactError, JobSpec, JobTarget
from vidcleaner.pipeline.stages import build_spec
from vidcleaner.pipeline.workspace import Workspace
from vidcleaner.settings_store import AppSettings, load_settings

__all__ = ["JobPlan", "plan_job", "target_for"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class JobPlan:
    spec: JobSpec
    ws: Workspace
    matcher: Matcher
    settings: AppSettings
    item: MediaItem
    title: Title | None
    completed: tuple[str, ...]
    """Stages already done in this work dir, after any invalidation."""
    invalidated: str | None = None
    """Why the previous work dir was discarded, if it was."""


def target_for(item: MediaItem, title: Title | None) -> JobTarget:
    """Which library item this job is for. Ids only -- never credentials."""
    arr_app: str | None = None
    if title is not None and title.arr_id is not None and title.arr_id >= 0:
        arr_app = "sonarr" if title.kind == "series" else "radarr"
    return JobTarget(
        media_item_id=item.id,
        title_id=item.title_id,
        kind=item.kind,  # type: ignore[arg-type]
        arr_app=arr_app,  # type: ignore[arg-type]
        arr_id=title.arr_id if arr_app else None,
        arr_file_id=item.arr_file_id,
        season=item.season,
        episode=item.episode,
        tvdb_id=title.tvdb_id if title else None,
        tmdb_id=title.tmdb_id if title else None,
    )


def plan_job(session: Session, job: Job, *, deploy: Settings | None = None) -> JobPlan:
    """Build everything the stage driver needs for one claimed job.

    The settings and profile snapshots are taken **now**, not when the job was
    enqueued: a few hundred backfill jobs may have sat in the queue while the user
    edited the word list, and they should run against what the user means today.
    """
    deploy = deploy or get_settings()
    item = session.get(MediaItem, job.media_item_id)
    if item is None:
        raise ValueError(f"job {job.id} points at media item {job.media_item_id}, which is gone")
    title = session.get(Title, item.title_id)

    settings = load_settings(session)
    matcher = matcher_for(
        session,
        title_id=item.title_id,
        item_id=item.id,
        profile_id=title.profile_id if title else None,
        settings=settings,
    )
    spec = build_spec(
        Path(item.path),
        profile=snapshot_for(matcher, settings),
        settings=settings,
        out=None,  # render writes /work/<uuid>/out.mkv; swap moves it into place
        dry_run=bool(job.dry_run),
        force=bool(job.force),
        stt_mode=job.stt_mode or "windowed",
        job_id=job.id,
        in_place=not job.dry_run,
        trigger=job.trigger,
        target=target_for(item, title),
    )

    ws = Workspace.for_job(job.id, deploy)
    invalidated = _invalidate_if_stale(ws, spec)
    ws.ensure()
    return JobPlan(
        spec=spec,
        ws=ws,
        matcher=matcher,
        settings=settings,
        item=item,
        title=title,
        completed=ws.completed_stages(),
        invalidated=invalidated,
    )


#: Fields of ``job.json`` that, if they changed, make every existing artifact in the
#: work dir describe a different computation. ``force`` and ``dry_run`` are absent
#: deliberately: they change what we *do next*, not what the artifacts mean.
_RESUME_KEYS = ("profile_hash", "stt_mode", "source_path", "version")


def _invalidate_if_stale(ws: Workspace, spec: JobSpec) -> str | None:
    if not ws.job_spec.is_file():
        return None
    try:
        previous = JobSpec.read(ws.job_spec)
    except ArtifactError:
        ws.clear_all()
        return "job.json could not be read"

    def value(candidate: JobSpec, key: str) -> object:
        return candidate.profile_hash if key == "profile_hash" else getattr(candidate, key)

    changed = [key for key in _RESUME_KEYS if value(previous, key) != value(spec, key)]
    if not changed:
        return None

    ws.clear_all()
    reason = ", ".join(changed)
    log.info("resume.invalidated", job_id=spec.job_id, changed=changed)
    return reason
