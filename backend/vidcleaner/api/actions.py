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
from vidcleaner.db.session import get_db, utcnow
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
    item_ids: list[int] | None = Field(default=None, min_length=1, max_length=1000)
    """§9.3's picker: act on just these files instead of the whole title.

    ``None`` means every file, which is what the title-level buttons have always
    meant. An **empty list is rejected** rather than read as "all" -- a selection UI
    that sends nothing must not clean the library. The cap is the same reasoning as
    `sync.CHUNK_SIZE`: this loop runs inside the request's single transaction.
    """


class ActionResult(BaseModel):
    action: str
    selected: bool = False
    """True when ``item_ids`` narrowed the action to part of a title."""
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
    """§2: marking a *movie* enqueues its existing file."""
    deferred: int = 0
    """Pre-existing files a *series* brought in unselected, for the UI to report.
    Often 0 even for a large series -- the files it already has are usually not in
    the database yet at this point; `titles.backfill_from` is what marks those when
    the sync creates them."""


class TitleSyncResult(BaseModel):
    items: int = 0
    added: int = 0
    deferred: int = 0
    queued: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class JobPatch(BaseModel):
    priority: int = Field(ge=0, le=10_000)


class WhitelistRequest(BaseModel):
    canonical_word: str = Field(min_length=1, max_length=200)
    scope: Literal["global", "title", "item"] = "item"
    context_text: str | None = None
    mode: Literal["suppress", "allow"] = "suppress"
    """§9.4's button always suppresses; ``allow`` exists so a narrower scope can undo
    a broader rule (M5). Narrowest scope wins."""
    reprocess: bool = True
    """§9.4's flow is "false positive -> whitelist -> reprocess"; the checkbox exists
    so a user cleaning up a dozen words at once can queue one job at the end."""


class WhitelistResult(BaseModel):
    id: int
    scope: str
    scope_id: int | None
    canonical_word: str
    context_text: str | None = None
    mode: str = "suppress"
    created: bool = True
    job_id: str | None = None


# --------------------------------------------------------------------- helpers


def _enqueue_items(
    session: Session, items: list[MediaItem], action: str, *, trigger: str | None = None
) -> ActionResult:
    """``trigger`` overrides the queue trigger without renaming the action, so
    ``ActionResult.action`` still reports the button the user pressed."""
    result = ActionResult(action=action, considered=len(items))
    kwargs = dict(ENQUEUE_KW[action])
    if trigger is not None:
        kwargs["trigger"] = trigger
    for item in items:
        outcome = queue.enqueue(
            session,
            media_item_id=item.id,
            priority=DEFAULT_PRIORITY.get(kwargs["trigger"], 100),
            **kwargs,
        )
        if outcome.created:
            result.queued.append(outcome.job_id)
            if not kwargs.get("dry_run"):
                # Asking for a file is opting it in: the flag must not put it back
                # out of the hourly pass's reach, which is also what keeps a `failed`
                # job retryable. A dry run never touches the library, so it is not an
                # opt-in and deliberately leaves the flag alone.
                item.skip_backfill = False
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


def _apply(
    session: Session, items: list[MediaItem], action: str, *, selected: bool = False
) -> ActionResult:
    if action == "restore":
        return _restore_items(session, items)
    return _enqueue_items(
        session, items, action, trigger="backfill" if selected and action == "process" else None
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
    deferred = 0
    if patch.enabled is not None and patch.enabled != title.enabled:
        title.enabled = patch.enabled
        db.flush()
        if patch.enabled:
            from vidcleaner.integrations.sync import (  # noqa: PLC0415
                backfill_title,
                defer_existing_items,
            )

            if title.kind == "series":
                # §2 as amended in M7: the episodes this series already has are
                # assumed watched and arrive **unselected**; the user picks them on
                # the Title page. The watermark is what marks the ones the sync has
                # not created yet -- which is most of them, since items are pulled
                # only for titles that are already enabled.
                title.backfill_from = utcnow()
                db.flush()
                deferred = defer_existing_items(db, title)
            else:
                queued = backfill_title(db, title, settings=load_settings(db))
    db.flush()
    return TitlePatchResult(
        id=title.id,
        enabled=title.enabled,
        profile_id=title.profile_id,
        queued=queued,
        deferred=deferred,
    )


@router.post("/library/titles/{title_id}/sync", response_model=TitleSyncResult)
def sync_title(title_id: int, db: DbSession) -> TitleSyncResult:
    """Pull one title's files from its arr now.

    The UI calls this straight after enabling a title, so §9.3's picker has files to
    show instead of waiting up to an hour for the periodic pass; it is also the Title
    page's "Refresh from Sonarr". The work happens in
    :func:`integrations.sync.sync_one_title`, which opens its **own** sessions -- a
    request-scoped transaction spanning an arr round trip would hold SQLite's write
    lock past the worker's 5 s busy timeout and break its claim.
    """
    from vidcleaner.integrations import from_database  # noqa: PLC0415
    from vidcleaner.integrations.sync import sync_one_title  # noqa: PLC0415

    if db.get(Title, title_id) is None:
        raise HTTPException(status_code=404, detail=f"no title {title_id}")
    db.commit()  # release this request's transaction before the HTTP round trip
    bundle = from_database(db)
    try:
        report = sync_one_title(title_id, integrations=bundle)
    finally:
        bundle.close()
    return TitleSyncResult(
        items=report.items_seen,
        added=report.items_added,
        deferred=report.items_deferred,
        queued=report.enqueued,
        errors=report.errors,
    )


@router.post("/library/titles/{title_id}/actions", response_model=ActionResult)
def title_action(
    title_id: int, request: Annotated[ActionRequest, Body()], db: DbSession
) -> ActionResult:
    """§9.3's "process now / reprocess all / restore originals / dry-run", and since
    M7 the same verbs over a **selection** of the title's files.

    A selection enqueues as ``backfill`` (priority 200) rather than ``manual`` (50):
    ticking a whole series is a backfill by definition, and it must not push ahead of
    an episode Sonarr just imported. The title-level buttons keep their priority --
    they say "all", and the user pressing one is asking for exactly that.
    """
    title = db.get(Title, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail=f"no title {title_id}")

    items = _items_of(db, title_id)
    if request.item_ids is None:
        return _apply(db, items, request.action)

    wanted = list(dict.fromkeys(request.item_ids))
    by_id = {item.id: item for item in items}
    missing = [item_id for item_id in wanted if item_id not in by_id]
    if missing:
        # 422, not 404: the title exists, the request body is what is wrong. Refusing
        # rather than silently dropping them -- a picker that acts on a subset of what
        # the user ticked is worse than one that errors.
        raise HTTPException(
            status_code=422,
            detail=f"item(s) {missing} do not belong to title {title_id}",
        )
    result = _apply(db, [by_id[item_id] for item_id in wanted], request.action, selected=True)
    result.selected = True
    return result


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
            WhitelistEntry.mode == request.mode,
            WhitelistEntry.scope_id.is_(scope_id)
            if scope_id is None
            else WhitelistEntry.scope_id == scope_id,
        )
    ).first()
    created = existing is None
    entry = existing or WhitelistEntry(
        scope=request.scope,
        scope_id=scope_id,
        canonical_word=word,
        context_text=context,
        mode=request.mode,
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
        mode=entry.mode,
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
