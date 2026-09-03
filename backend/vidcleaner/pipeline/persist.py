"""Recording a pipeline run, and its backups, in the database.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.constants import TERMINAL_STATES
from vidcleaner.db.models import Backup, Job, MediaItem, Title
from vidcleaner.db.models import Detection as DetectionRow
from vidcleaner.db.session import utcnow
from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import (
    DetectionResult,
    JobSpec,
    ProbeResult,
    SnippetsResult,
    SwapResult,
)

__all__ = [
    "LOCAL_TITLE_ARR_ID",
    "LOCAL_TITLE_NAME",
    "PersistResult",
    "ReconcileReport",
    "RestoreReport",
    "ensure_local_title",
    "ensure_media_item",
    "persist_run",
    "persist_swap",
    "reconcile_backups",
    "restore_item",
]

log = get_logger(__name__)

#: Negative so it can never collide with a real Sonarr/Radarr id.
LOCAL_TITLE_ARR_ID = -1
LOCAL_TITLE_NAME = "Local files (CLI)"
#: The `media_items` row unidentifiable backup files hang from.
ORPHAN_PATH = "<orphaned backups>"
#: Used only to tell a video backup from its sidecars within one job's rows.
SUBTITLE_SUFFIXES = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"})


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
    snippets: SnippetsResult | None = None,
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
    if state in TERMINAL_STATES:
        # The same rule `claim.set_state` applies. Without it a finished job keeps
        # `claimed_by`, so the Queue page shows it as still owned by a worker --
        # found by the run-loop test, not by review.
        job.claimed_by = None
        job.heartbeat = None
    session.flush()

    # `snippets` is keyed by the detection's index in `detections.json`, which is the
    # order they are written in here -- the two artifacts are read from one work dir.
    clips = snippets.by_index() if snippets else {}
    count = 0
    for index, detection in enumerate(detections.detections if detections else []):
        payload = detection.model_dump(exclude={"suspicious_reason"})
        clip = clips.get(index)
        if clip is not None:
            # Relative to `Settings.snippets_dir`, which the stage keys by job id.
            payload["snippet_path"] = f"{job.id}/{clip.rel_dir}"
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


# ------------------------------------------------------------------- backups


@dataclass(frozen=True, slots=True)
class RestoreReport:
    media_item_id: int
    restored_path: str
    displaced_path: str | None
    sidecars: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    adopted: int = 0
    """Files under /backups with no row -- a rename committed but its commit did not."""
    purged: int = 0
    """Rows whose backup file is gone."""


def persist_swap(
    session: Session,
    swap: SwapResult,
    *,
    media_item_id: int,
    job_id: str | None = None,
    retention_days: int = 30,
) -> list[int]:
    """One ``backups`` row per file moved -- the video and each sidecar.

    §5's `backups` columns are scalars, which reads as one row per job; several rows
    sharing a ``job_id`` is the natural fit and is what lets ``restore_item`` put the
    subtitles back too.

    Also brings ``media_items`` in line with what is now on disk: the path changes
    when an MP4 became an MKV, and any *earlier* kept backup for this item is now
    ``orphaned`` because the library file it belonged to no longer exists.
    """
    purge_after = utcnow() + timedelta(days=retention_days) if retention_days else None

    for previous in session.scalars(
        select(Backup).where(Backup.media_item_id == media_item_id, Backup.state == "kept")
    ):
        previous.state = "orphaned"
        previous.purge_after = purge_after

    ids: list[int] = []
    rows = [
        Backup(
            job_id=job_id,
            media_item_id=media_item_id,
            original_path=swap.original_path,
            backup_path=swap.backup_path,
            size=swap.backup_size,
            sha1_prefix=swap.backup_sha1_prefix,
            state="kept",
            purge_after=purge_after,
        )
    ]
    rows += [
        Backup(
            job_id=job_id,
            media_item_id=media_item_id,
            original_path=sidecar.original_path,
            backup_path=sidecar.backup_path,
            state="kept",
            purge_after=purge_after,
        )
        for sidecar in swap.sidecars
    ]
    for row in rows:
        session.add(row)
    session.flush()
    ids = [row.id for row in rows]

    item = session.get(MediaItem, media_item_id)
    if item is not None:
        item.path = swap.final_path
        item.size = swap.out_size or item.size
        item.status = "clean"
        item.cleaned_at = utcnow()
    session.flush()
    log.info(
        "persist.swap",
        media_item_id=media_item_id,
        final=swap.final_path,
        backups=len(ids),
    )
    return ids


def restore_item(session: Session, media_item_id: int, *, fs: Any = None) -> RestoreReport:
    """Put the original back (§9.3), video and sidecars together.

    The filesystem work is in ``pipeline/swap.py`` so CLAUDE.md's "library files are
    only ever changed by swap.py" stays literally true; the row bookkeeping is here,
    which is already the one place pipeline results become rows.
    """
    from vidcleaner.pipeline.swap import RestorePlan, restore_backup  # noqa: PLC0415

    item = session.get(MediaItem, media_item_id)
    if item is None:
        raise ValueError(f"no media item {media_item_id}")

    kept = session.scalars(
        select(Backup)
        .where(Backup.media_item_id == media_item_id, Backup.state == "kept")
        .order_by(Backup.created_at.desc(), Backup.id.desc())
    ).all()
    if not kept:
        raise ValueError(f"media item {media_item_id} has no kept backup to restore")

    sidecars = [b for b in kept if Path(b.backup_path).suffix.lower() in SUBTITLE_SUFFIXES]
    videos = [b for b in kept if b not in sidecars]
    if not videos:
        raise ValueError(f"media item {media_item_id} has no video backup to restore")
    video = videos[0]

    # Where the original goes back: the item's **current** name (a Rename webhook
    # may have moved it since the swap -- restoring to `original_path` would
    # recreate a stale filename) with the original's **extension** (an MP4 that
    # became an MKV must go back as an MP4).
    current = Path(item.path)
    target = current.with_suffix(Path(video.original_path).suffix)

    result = restore_backup(
        RestorePlan(
            backup_path=Path(video.backup_path),
            target_path=target,
            # The cleaned file is moved aside, never deleted. For the MP4 case this
            # is what stops the arr seeing both a .mp4 and a .mkv. It goes to
            # `/backups`, beside the original it replaced, rather than staying in
            # the library as a source-sized file nothing will ever clean up.
            displace_path=current,
            displace_to=Path(video.backup_path).with_name(current.name),
            expect_size=video.size or 0,
            expect_sha1_prefix=video.sha1_prefix or "",
        ),
        fs=fs,
    )
    video.state = "restored"

    restored_sidecars = 0
    warnings = list(result.warnings)
    for backup in sidecars:
        original = Path(backup.original_path)
        try:
            restore_backup(
                RestorePlan(
                    backup_path=Path(backup.backup_path),
                    target_path=original,
                    displace_path=original,
                    # Same reasoning as the video above: the redacted copy goes to
                    # `/backups`, not back into the library as `<name>.srt.cleaned`
                    # for nobody to ever clean up. Found by the M4 demo.
                    # (`_free_name` appends the `.cleaned` suffix itself.)
                    displace_to=Path(backup.backup_path).with_name(original.name),
                ),
                fs=fs,
            )
        except Exception as exc:  # noqa: BLE001 - a subtitle must not fail a restore
            warnings.append(f"could not restore {backup.original_path}: {exc}")
            continue
        backup.state = "restored"
        restored_sidecars += 1

    item.path = result.restored_path
    item.size = video.size or item.size
    item.status = "restored"
    item.cleaned_at = None
    session.flush()
    log.info(
        "persist.restored",
        media_item_id=media_item_id,
        path=result.restored_path,
        displaced=result.displaced_path,
        sidecars=restored_sidecars,
    )
    return RestoreReport(
        media_item_id=media_item_id,
        restored_path=result.restored_path,
        displaced_path=result.displaced_path,
        sidecars=restored_sidecars,
        warnings=tuple(warnings),
    )


def reconcile_backups(
    session: Session, backups_dir: Path, *, retention_days: int = 30
) -> ReconcileReport:
    """Close the one window the swap cannot: a committed rename whose row was lost.

    The rename and the SQLite commit cannot be made atomic, so a crash between them
    leaves a file in `/backups` that nothing knows about -- and a row whose file a
    human deleted leaves the reverse. Adding a `pending` backup state would only move
    the window, not close it; the intent journal in `/work` is the honest source of
    truth during a swap and this is the backstop afterwards.
    """
    known = {row.backup_path for row in session.scalars(select(Backup))}
    purge_after = utcnow() + timedelta(days=retention_days) if retention_days else None

    adopted = 0
    if backups_dir.is_dir():
        for path in sorted(backups_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if str(path) in known:
                continue
            session.add(
                Backup(
                    media_item_id=_orphan_item_id(session),
                    original_path="",
                    backup_path=str(path),
                    size=path.stat().st_size,
                    state="orphaned",
                    purge_after=purge_after,
                )
            )
            adopted += 1

    purged = 0
    for row in session.scalars(select(Backup).where(Backup.state.in_(("kept", "orphaned")))):
        if not Path(row.backup_path).exists():
            row.state = "purged"
            purged += 1

    session.flush()
    if adopted or purged:
        log.info("backups.reconciled", adopted=adopted, purged=purged)
    return ReconcileReport(adopted=adopted, purged=purged)


def _orphan_item_id(session: Session) -> int:
    """`backups.media_item_id` is NOT NULL, and an adopted file belongs to nothing we
    can identify -- so it hangs from the same sentinel the CLI's own runs use."""
    title = ensure_local_title(session)
    item = session.scalars(
        select(MediaItem).where(MediaItem.title_id == title.id, MediaItem.path == ORPHAN_PATH)
    ).first()
    if item is None:
        item = MediaItem(title_id=title.id, kind="movie", path=ORPHAN_PATH, status="untracked")
        session.add(item)
        session.flush()
    return item.id
