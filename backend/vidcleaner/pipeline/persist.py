"""Recording a CLI run in the database.

Kept deliberately outside the stages: CLAUDE.md requires each stage to be a
pure function of its on-disk inputs, so threading a ``Session`` through
``StageContext`` would make every stage test need a migrated database and make
"resume from markers" ambiguous about where the truth lives. The CLI runs the
pipeline first and calls this afterwards, reading the same artifacts anyone else
would.

The awkward part is the schema. ``detections.media_item_id`` and
``jobs.media_item_id`` are non-null foreign keys up a chain
(``detections -> jobs -> media_items -> titles``) that Sonarr and Radarr will not
populate until M3. A CLI run on an arbitrary file therefore needs somewhere to
hang, which is the sentinel title below. **M3 owes a reconciliation step**: when
a real arr file matches a local row's ``path``, it should adopt that row rather
than inserting a duplicate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.models import Detection as DetectionRow
from vidcleaner.db.models import Job, MediaItem, Title
from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import DetectionResult, JobSpec, ProbeResult

__all__ = [
    "LOCAL_TITLE_ARR_ID",
    "LOCAL_TITLE_NAME",
    "PersistResult",
    "ensure_local_title",
    "ensure_media_item",
    "persist_run",
]

log = get_logger(__name__)

#: Negative so it can never collide with a real Sonarr/Radarr id.
LOCAL_TITLE_ARR_ID = -1
LOCAL_TITLE_NAME = "Local files (CLI)"


@dataclass(frozen=True, slots=True)
class PersistResult:
    job_id: str
    media_item_id: int
    detections: int


def ensure_local_title(session: Session) -> Title:
    """The sentinel title CLI runs hang from. Idempotent.

    ``uq_titles_kind_arr_id`` makes the lookup exact, and ``enabled=False``
    keeps M3's backfill from ever picking these up as if they were a real
    Sonarr series.
    """
    title = session.scalars(
        select(Title).where(Title.kind == "movie", Title.arr_id == LOCAL_TITLE_ARR_ID)
    ).first()
    if title is None:
        title = Title(
            kind="movie",
            arr_id=LOCAL_TITLE_ARR_ID,
            title=LOCAL_TITLE_NAME,
            enabled=False,
        )
        session.add(title)
        session.flush()
        log.info("persist.local_title_created", title_id=title.id)
    return title


def ensure_media_item(session: Session, probe: ProbeResult) -> MediaItem:
    """Find or create the row for this file, keyed on path.

    ``season`` and ``episode`` stay NULL. SQLite treats NULLs as distinct in a
    unique constraint, so ``uq_media_items_title_s_e`` permits many such rows.
    """
    item = session.scalars(select(MediaItem).where(MediaItem.path == probe.path)).first()
    if item is None:
        item = MediaItem(
            title_id=ensure_local_title(session).id,
            kind="movie",
            path=probe.path,
            status="untracked",
        )
        session.add(item)

    item.size = probe.size
    item.duration = probe.duration
    item.source_fingerprint = probe.fingerprint
    session.flush()
    return item


def persist_run(
    session: Session,
    spec: JobSpec,
    *,
    probe: ProbeResult,
    detections: DetectionResult | None,
    state: str,
    stage: str | None = None,
    error: str | None = None,
    model_used: str | None = None,
    subtitle_source: str | None = None,
    timings: dict[str, float] | None = None,
    work_dir: Path | None = None,
    media_item: MediaItem | None = None,
    count_attempt: bool = True,
) -> PersistResult:
    """Write the ``jobs`` row and its ``detections``, replacing any earlier run.

    ``media_item`` short-circuits the path lookup: a worker job already knows which
    row it is for (it came from the queue), and looking it up by path again would
    resolve the *post-swap* path to nothing.

    ``count_attempt=False`` is the worker path. ``jobs.attempts`` counts **claims**,
    and the queue already incremented it when it claimed the job; incrementing again
    here would retire every job after 1.5 real attempts.
    """
    item = media_item if media_item is not None else ensure_media_item(session, probe)

    job = session.get(Job, spec.job_id)
    if job is None:
        job = Job(id=spec.job_id, media_item_id=item.id, trigger=spec.trigger)
        session.add(job)
    else:
        # A re-run of the same deterministic job id replaces its detections
        # rather than accumulating them.
        for row in session.scalars(select(DetectionRow).where(DetectionRow.job_id == job.id)).all():
            session.delete(row)

    job.media_item_id = item.id
    job.state = state
    job.stage = stage
    job.progress_pct = 100.0 if state in {"done", "already_clean"} else job.progress_pct
    if count_attempt:
        job.attempts = (job.attempts or 0) + 1
    job.work_dir = str(work_dir) if work_dir else None
    job.source_fingerprint = probe.fingerprint
    job.stt_mode = spec.stt_mode
    job.model_used = model_used
    job.subtitle_source = subtitle_source
    job.profile_snapshot_json = spec.profile.model_dump_json()
    job.timings_json = json.dumps(timings or {})
    job.dry_run = spec.dry_run
    job.error = error
    job.started_at = job.started_at or spec.created_at or datetime.now(UTC)
    job.finished_at = datetime.now(UTC)
    session.flush()

    count = 0
    for detection in detections.detections if detections else []:
        payload = detection.model_dump(exclude={"suspicious_reason"})
        session.add(DetectionRow(job_id=job.id, media_item_id=item.id, **payload))
        count += 1

    item.last_job_id = job.id
    item.status = _item_status(state, spec.dry_run)
    if state == "done" and not spec.dry_run:
        item.cleaned_at = datetime.now(UTC)
    session.flush()

    log.info(
        "persist.done",
        job_id=job.id,
        media_item_id=item.id,
        state=state,
        detections=count,
        status=item.status,
    )
    return PersistResult(job.id, item.id, count)


def _item_status(state: str, dry_run: bool) -> str:
    if state == "already_clean":
        return "already_clean"
    if state == "failed":
        return "failed"
    if dry_run:
        return "pending"
    return "clean" if state == "done" else "pending"
