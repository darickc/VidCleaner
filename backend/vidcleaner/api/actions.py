"""Everything the M4 screens *do*: enable a title, queue work, restore, whitelist.

Reads live in :mod:`api.jobs`, :mod:`api.library` and :mod:`api.items`; this module is
the write side, kept together because the four verbs are the same on every screen and
the mapping from a button to a queue row should be stated exactly once.

Nothing here re-implements queue policy. ``worker.claim.enqueue`` decides whether a
job is created, deduped or superseded, and ``pipeline.persist.restore_item`` owns
putting a file back -- these handlers translate a button press into one of those calls
and report what happened.

Handlers are sync ``def`` for the same reason as :mod:`api.integrations`: FastAPI runs
them in a threadpool, so the blocking database (and, for a restore, the blocking
rename) costs a worker thread rather than the event loop.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.constants import DEFAULT_PRIORITY, WHITELIST_SCOPES
from vidcleaner.db.models import Backup, Job, MediaItem, Profile, Title, WhitelistEntry
from vidcleaner.db.session import get_db
from vidcleaner.logging import get_logger
from vidcleaner.matching.profile import clear_matcher_cache
from vidcleaner.settings_store import load_settings
from vidcleaner.worker import claim as queue

router = APIRouter(tags=["actions"])
DbSession = Annotated[Session, Depends(get_db)]
log = get_logger(__name__)

Action = Literal["process", "reprocess", "dry_run", "restore"]

#: How each button maps onto ``enqueue``. ``reprocess`` and ``dry_run`` force, because
#: the user is asking for work on a file we already consider done -- without it §4's
#: ``already_clean`` tag would answer instead, which is exactly the confusing outcome
#: the "reprocess" button exists to avoid.
ENQUEUE_KW: dict[str, dict[str, Any]] = {
    "process": {"trigger": "manual", "force": False},
    "reprocess": {"trigger": "reprocess", "force": True},
    "dry_run": {"trigger": "manual", "force": True, "dry_run": True},
}


class ActionRequest(BaseModel):
    action: Action = "process"


class ActionResult(BaseModel):
    action: str
    queued: list[str] = Field(default_factory=list)
    """Job ids created. Fewer than ``considered`` when items were already active."""
    skipped: dict[str, int] = Field(default_factory=dict)
    """Why nothing was queued for the rest: ``deduped``, ``already_active``, …"""
    restored: list[int] = Field(default_factory=list)
    considered: int = 0
    warnings: list[str] = Field(default_factory=list)


class TitlePatch(BaseModel):
    enabled: bool | None = None
    profile_id: int | None = None
    clear_profile: bool = False
    """``profile_id: null`` cannot mean "no change" and "use the default" at once."""


class TitlePatchResult(BaseModel):
    id: int
    enabled: bool
    profile_id: int | None = None
    queued: list[str] = Field(default_factory=list)
    """§2: marking a title enqueues its existing files."""


class JobPatch(BaseModel):
    priority: int = Field(ge=0, le=10_000)


class WhitelistRequest(BaseModel):
    canonical_word: str = Field(min_length=1, max_length=200)
    scope: Literal["global", "title", "item"] = "item"
    context_text: str | None = None
    reprocess: bool = True
    """§9.4's flow is "false positive -> whitelist -> reprocess"; the checkbox exists
    so a user cleaning up a dozen words at once can queue one job at the end."""


class WhitelistResult(BaseModel):
    id: int
    scope: str
    scope_id: int | None
    canonical_word: str
    context_text: str | None = None
    created: bool = True
    job_id: str | None = None


# --------------------------------------------------------------------- helpers


def _enqueue_items(session: Session, items: list[MediaItem], action: str) -> ActionResult:
    result = ActionResult(action=action, considered=len(items))
    kwargs = ENQUEUE_KW[action]
    for item in items:
        outcome = queue.enqueue(
            session,
            media_item_id=item.id,
            priority=DEFAULT_PRIORITY.get(kwargs["trigger"], 100),
            **kwargs,
        )
        if outcome.created:
            result.queued.append(outcome.job_id)
        else:
            result.skipped[outcome.reason] = result.skipped.get(outcome.reason, 0) + 1
    return result


def _restore_items(session: Session, items: list[MediaItem]) -> ActionResult:
    from vidcleaner.pipeline.persist import restore_item  # noqa: PLC0415

    result = ActionResult(action="restore", considered=len(items))
    for item in items:
        kept = session.scalars(
            select(Backup).where(Backup.media_item_id == item.id, Backup.state == "kept")
        ).first()
        if kept is None:
            result.skipped["no_backup"] = result.skipped.get("no_backup", 0) + 1
            continue
        try:
            report = restore_item(session, item.id)
        except (ValueError, RuntimeError, OSError) as exc:
            result.warnings.append(f"{item.path}: {exc}")
            continue
        result.restored.append(item.id)
        result.warnings.extend(report.warnings)
    if result.restored:
        result.warnings.extend(_notify_restored(session, result.restored))
    return result


def _notify_restored(session: Session, item_ids: list[int]) -> list[str]:
    """Tell the arrs and Jellyfin the files changed back.

    Without this the library keeps advertising a Clean track that no longer exists,
    which is precisely the failure §12's end-to-end check ("restore original and
    confirm reversal") is looking for. Best effort: the files are already correct, so
    every error is a warning, exactly as in the ``refresh`` stage.
    """
    from vidcleaner.integrations import from_database  # noqa: PLC0415
    from vidcleaner.integrations.jellyfin import MediaUpdate  # noqa: PLC0415

    warnings: list[str] = []
    bundle = from_database(session)
    try:
        rescanned: set[tuple[str, int]] = set()
        updates: list[MediaUpdate] = []
        for item_id in item_ids:
            item = session.get(MediaItem, item_id)
            if item is None:  # pragma: no cover - just restored it
                continue
            title = session.get(Title, item.title_id)
            app = None if title is None else ("sonarr" if title.kind == "series" else "radarr")
            client = bundle.arr(app) if app else None
            if client is not None and title is not None and (app, title.arr_id) not in rescanned:
                rescanned.add((app, title.arr_id))
                try:
                    client.rescan(title.arr_id)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{app} rescan failed: {exc}")
            if bundle.jellyfin is not None:
                remote = bundle.map_for("jellyfin").to_remote(item.path)
                updates.append(MediaUpdate(Path=remote, UpdateType="Modified"))
        if bundle.jellyfin is not None and updates:
            try:
                bundle.jellyfin.media_updated(updates)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"jellyfin refresh failed: {exc}")
    finally:
        bundle.close()
    return warnings


def _items_of(session: Session, title_id: int) -> list[MediaItem]:
    return list(
        session.scalars(
            select(MediaItem)
            .where(MediaItem.title_id == title_id)
            .order_by(MediaItem.season, MediaItem.episode, MediaItem.path)
        ).all()
    )


def _apply(session: Session, items: list[MediaItem], action: str) -> ActionResult:
    return (
        _restore_items(session, items)
        if action == "restore"
        else _enqueue_items(session, items, action)
    )


# ---------------------------------------------------------------------- titles


@router.patch("/library/titles/{title_id}", response_model=TitlePatchResult)
def patch_title(
    title_id: int, patch: Annotated[TitlePatch, Body()], db: DbSession
) -> TitlePatchResult:
    """§9.2's Clean toggle and profile dropdown.

    Turning a title on backfills it immediately (§2/§8) rather than waiting for the
    hourly sync: the user just told us they want this series cleaned, and an hour of
    apparently nothing happening is how a feature gets reported as broken.
    """
    title = db.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail=f"no title {title_id}")

    if patch.profile_id is not None:
        if db.get(Profile, patch.profile_id) is None:
            raise HTTPException(status_code=422, detail=f"no profile {patch.profile_id}")
        title.profile_id = patch.profile_id
    elif patch.clear_profile:
        title.profile_id = None

    queued: list[str] = []
    if patch.enabled is not None and patch.enabled != title.enabled:
        title.enabled = patch.enabled
        db.flush()
        if patch.enabled:
            from vidcleaner.integrations.sync import backfill_title  # noqa: PLC0415

            queued = backfill_title(db, title, settings=load_settings(db))
    db.flush()
    return TitlePatchResult(
        id=title.id, enabled=title.enabled, profile_id=title.profile_id, queued=queued
    )


@router.post("/library/titles/{title_id}/actions", response_model=ActionResult)
def title_action(
    title_id: int, request: Annotated[ActionRequest, Body()], db: DbSession
) -> ActionResult:
    """§9.3's "process now / reprocess all / restore originals / dry-run"."""
    title = db.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail=f"no title {title_id}")
    return _apply(db, _items_of(db, title_id), request.action)


# ----------------------------------------------------------------------- items


@router.post("/items/{item_id}/actions", response_model=ActionResult)
def item_action(
    item_id: int, request: Annotated[ActionRequest, Body()], db: DbSession
) -> ActionResult:
    item = db.get(MediaItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no media item {item_id}")
    return _apply(db, [item], request.action)


@router.post("/items/{item_id}/whitelist", response_model=WhitelistResult)
def add_whitelist(
    item_id: int, request: Annotated[WhitelistRequest, Body()], db: DbSession
) -> WhitelistResult:
    """§9.4's "false positive -> whitelist (item/title/global) + reprocess"."""
    item = db.get(MediaItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no media item {item_id}")

    scope_id = {"global": None, "title": item.title_id, "item": item.id}[request.scope]
    word = request.canonical_word.strip().lower()
    context = request.context_text.strip() if request.context_text else None

    existing = db.scalars(
        select(WhitelistEntry).where(
            WhitelistEntry.scope == request.scope,
            WhitelistEntry.canonical_word == word,
            WhitelistEntry.scope_id.is_(scope_id)
            if scope_id is None
            else WhitelistEntry.scope_id == scope_id,
        )
    ).first()
    created = existing is None
    entry = existing or WhitelistEntry(
        scope=request.scope, scope_id=scope_id, canonical_word=word, context_text=context
    )
    if created:
        db.add(entry)
    db.flush()
    if created:
        clear_matcher_cache()

    job_id = None
    if request.reprocess:
        # Forced: the file is `clean` for the old profile hash, and the whole point is
        # to redo it under the new one.
        outcome = queue.enqueue(
            db,
            media_item_id=item.id,
            trigger="reprocess",
            force=True,
            priority=DEFAULT_PRIORITY["reprocess"],
        )
        job_id = outcome.job_id
    return WhitelistResult(
        id=entry.id,
        scope=entry.scope,
        scope_id=entry.scope_id,
        canonical_word=entry.canonical_word,
        context_text=entry.context_text,
        created=created,
        job_id=job_id,
    )


@router.delete("/whitelist/{entry_id}", status_code=204)
def delete_whitelist(entry_id: int, db: DbSession) -> None:
    entry = db.get(WhitelistEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no whitelist entry {entry_id}")
    if entry.scope not in WHITELIST_SCOPES:  # pragma: no cover - defensive
        raise HTTPException(status_code=422, detail="unknown scope")
    db.delete(entry)
    db.flush()
    clear_matcher_cache()


# ------------------------------------------------------------------------ jobs


@router.post("/jobs/{job_id}/cancel", response_model=dict)
def cancel_job(job_id: str, db: DbSession) -> dict:
    """A queued job goes straight to ``cancelled``; a running one is asked to stop
    and its worker notices between stages (``claim.should_abort``)."""
    if db.get(Job, job_id) is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return {"cancelled": queue.cancel(db, job_id, reason="cancelled from the queue page")}


@router.post("/jobs/{job_id}/retry", response_model=ActionResult)
def retry_job(job_id: str, db: DbSession) -> ActionResult:
    """Re-run a finished job's item. A new row, not a resurrection of the old one:
    ``jobs`` is the record of what ran, and its work dir may already be pruned."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    item = db.get(MediaItem, job.media_item_id)
    if item is None:  # pragma: no cover - cascade deletes these together
        raise HTTPException(status_code=404, detail="the job's media item is gone")
    return _enqueue_items(db, [item], "reprocess")


@router.patch("/jobs/{job_id}", response_model=dict)
def patch_job(job_id: str, patch: Annotated[JobPatch, Body()], db: DbSession) -> dict:
    """§9.1's reorder. Only a queued job can move: once claimed, the order is spent."""
    if db.get(Job, job_id) is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id}")
    return {"updated": queue.reprioritize(db, job_id, patch.priority)}
