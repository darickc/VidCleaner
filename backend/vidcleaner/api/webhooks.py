"""Sonarr/Radarr webhook receivers -- PLAN.md §6 step 0 and §8.

Three properties this endpoint has to have, and the reasons are all in §3:

* **Always 200, except 401/400.** Sonarr disables a notification after repeated
  failures, so an unknown `eventType` must not turn it red.
* **Respond immediately.** Dispatch is therefore **pure database** -- no outbound
  HTTP at all. An unknown title is recorded and left for the hourly sync to adopt,
  rather than blocking the receiver on a possibly-hung Sonarr. That is what makes
  "respond 200 immediately" true without a BackgroundTask.
* **`Test` is handled before anything else.** A Test payload carries dummy ids
  (`series.id = 1`), so resolving the title first would let a Test click enqueue a
  real job against a fake path on any install whose series 1 exists.

The shared secret is the *entire* security perimeter, because the application has no
user authentication at all and PLAN.md never says so. `webhook_token` is therefore
generated at seed time so the receiver can always require it -- the alternative,
accepting when unconfigured, is an unauthenticated job-enqueue endpoint on first boot.
"""

from __future__ import annotations

import hmac
import json
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.constants import TERMINAL_STATES
from vidcleaner.db.models import Backup, Job, MediaItem, Title, WebhookEvent
from vidcleaner.db.session import get_db, utcnow
from vidcleaner.integrations.pathmap import load_path_map
from vidcleaner.integrations.payloads import parse_webhook
from vidcleaner.integrations.sync import SyncReport, resolve_item
from vidcleaner.logging import get_logger
from vidcleaner.settings_store import load_settings
from vidcleaner.worker.claim import cancel, enqueue

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
DbSession = Annotated[Session, Depends(get_db)]
log = get_logger(__name__)

TOKEN_HEADER = "X-VidCleaner-Token"
MAX_BODY_BYTES = 1024 * 1024
DEDUPE_WINDOW_S = 60.0

#: The events that identify a library item, and so need the title resolved first.
NEEDS_TITLE = frozenset({"Download", "Rename", "EpisodeFileDelete", "MovieFileDelete"})

#: §3's list. Anything absent is stored and acknowledged, never an error.
STORE_ONLY = frozenset(
    {
        "Grab",
        "Health",
        "HealthRestored",
        "ApplicationUpdate",
        "ManualInteractionRequired",
        "Retag",
    }
)


class SetupInfo(BaseModel):
    url: str
    header_name: str
    token: str
    note: str


@router.get("/setup", response_model=SetupInfo)
def webhook_setup(request: Request, db: DbSession, app: str = "sonarr") -> SetupInfo:
    """What §8's setup page shows the user to paste.

    Hands out the token in plaintext, which is only acceptable because the whole app
    is unauthenticated and expected to sit behind a reverse proxy -- see the README.
    """
    settings = load_settings(db)
    base = str(request.base_url).rstrip("/")
    return SetupInfo(
        url=f"{base}/api/webhooks/{app}",
        header_name=TOKEN_HEADER,
        token=settings.webhook_token,
        note="Paste the header as `Name=Value` in the arr's Webhook notification settings.",
    )


@router.post("/install")
def install_webhook(request: Request, db: DbSession, app: str = "sonarr") -> dict[str, Any]:
    """§8: "can create the notification via POST /api/v3/notification on user click"."""
    from vidcleaner.integrations import from_database
    from vidcleaner.integrations.base import IntegrationError

    if app not in ("sonarr", "radarr"):
        raise HTTPException(status_code=404, detail=f"unknown app {app!r}")
    settings = load_settings(db)
    bundle = from_database(db)
    client = bundle.arr(app)
    if client is None:
        bundle.close()
        raise HTTPException(status_code=409, detail=f"{app} is not configured")
    url = f"{str(request.base_url).rstrip('/')}/api/webhooks/{app}"
    try:
        existing = [n for n in client.list_notifications() if n.url == url]
        if existing:
            return {"created": False, "id": existing[0].id, "url": url}
        notification = client.create_webhook_notification(
            url, settings.webhook_token, header=TOKEN_HEADER
        )
    except IntegrationError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    finally:
        bundle.close()
    return {"created": True, "id": notification.id, "url": url}


@router.post("/sonarr")
async def sonarr_webhook(request: Request, db: DbSession, response: Response) -> dict[str, Any]:
    return await _receive("sonarr", request, db, response)


@router.post("/radarr")
async def radarr_webhook(request: Request, db: DbSession, response: Response) -> dict[str, Any]:
    return await _receive("radarr", request, db, response)


async def _receive(
    source: str, request: Request, db: Session, response: Response
) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    settings = load_settings(db)
    token = request.headers.get(TOKEN_HEADER, "")
    if not settings.webhook_token or not hmac.compare_digest(token, settings.webhook_token):
        # Nothing is stored: writing unauthenticated bodies to the database would be
        # a denial-of-service vector on an endpoint anyone can reach.
        log.warning("webhook.unauthorised", source=source, has_header=bool(token))
        raise HTTPException(status_code=401, detail="bad or missing webhook token")

    try:
        payload = json.loads(body or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body is not a JSON object")

    event = WebhookEvent(
        source=source,
        event_type=str(payload.get("eventType") or "")[:48] or None,
        payload_json=body.decode("utf-8", errors="replace"),  # §6.0: store it raw
    )
    db.add(event)
    db.flush()

    try:
        note, job_id = _dispatch(source, payload, db)
        event.handled = True
    except Exception as exc:  # noqa: BLE001 - never fail the arr's delivery
        log.exception("webhook.dispatch_failed", source=source, event_type=event.event_type)
        event.handled = False
        event.note = str(exc)[:500]
        db.flush()
        return {"ok": False, "note": event.note}

    event.note = note
    event.job_id = job_id
    db.flush()
    log.info(
        "webhook.handled",
        source=source,
        event_type=event.event_type,
        note=note,
        job=job_id,
    )
    return {"ok": True, "note": note, "job_id": job_id}


# ------------------------------------------------------------------ dispatch


def _dispatch(source: str, payload: dict[str, Any], db: Session) -> tuple[str, str | None]:
    hook = parse_webhook(source, payload)
    event = hook.event_type

    # First, before any title resolution: Test payloads carry dummy ids.
    if event == "Test":
        return "test", None
    if not event:
        return "no event type", None
    if event in STORE_ONLY:
        return f"stored:{event}", None

    if event in ("SeriesAdd", "MovieAdded"):
        _upsert_title(hook, db)
        return f"title upserted:{event}", None
    if event in ("SeriesDelete", "MovieDelete"):
        return _on_title_delete(hook, db), None

    if event not in NEEDS_TITLE:
        # Answered before resolving the title, so an unrecognised event type reads
        # as exactly that rather than as `unknown_title` -- the event type is the
        # more useful fact when a new arr release adds one.
        return f"unhandled:{event}", None

    title = _find_title(hook, db)
    if title is None:
        # The hourly sync will adopt it; blocking the receiver on the arr's API to
        # find out about it would defeat "respond 200 immediately".
        return "unknown_title", None

    if event == "Rename":
        return _on_rename(hook, db, title), None
    if event in ("EpisodeFileDelete", "MovieFileDelete"):
        return _on_file_delete(hook, db, title), None
    return _on_download(hook, db, title)


def _find_title(hook: Any, db: Session) -> Title | None:
    if hook.arr_id is None:
        return None
    return db.scalars(
        select(Title).where(Title.kind == hook.kind, Title.arr_id == hook.arr_id)
    ).first()


def _upsert_title(hook: Any, db: Session) -> Title | None:
    if hook.arr_id is None:
        return None
    pathmap = load_path_map(db, "sonarr" if hook.kind == "series" else "radarr")
    title = _find_title(hook, db)
    if title is None:
        # Created **disabled**: §2 has the user mark titles for cleaning.
        title = Title(kind=hook.kind, arr_id=hook.arr_id, title="", enabled=False)
        db.add(title)
    remote = hook.series if hook.kind == "series" else hook.movie
    if remote is not None:
        title.title = remote.title or title.title
        title.year = remote.year
        title.imdb_id = remote.imdb_id or title.imdb_id
        if hook.kind == "series":
            title.tvdb_id = remote.tvdb_id or title.tvdb_id
            folder = remote.path
        else:
            title.tmdb_id = remote.tmdb_id or title.tmdb_id
            folder = remote.folder_path
        if folder:
            title.arr_path = pathmap.to_local(folder)
    db.flush()
    return title


def _on_title_delete(hook: Any, db: Session) -> str:
    title = _find_title(hook, db)
    if title is None:
        return "unknown_title"
    title.enabled = False
    if hook.deleted_files:
        for item in db.scalars(select(MediaItem).where(MediaItem.title_id == title.id)):
            item.status = "stale"
            _orphan_backups(db, item.id)
    db.flush()
    return "title disabled"


def _on_rename(hook: Any, db: Session, title: Title) -> str:
    """Keyed on `arr_file_id`, which is authoritative; `previousPath` is the fallback."""
    pathmap = load_path_map(db, "sonarr" if hook.kind == "series" else "radarr")
    renamed = 0
    for entry in hook.renamed:
        item = None
        if entry.id is not None:
            item = db.scalars(
                select(MediaItem).where(
                    MediaItem.title_id == title.id, MediaItem.arr_file_id == entry.id
                )
            ).first()
        if item is None and entry.previous_path:
            item = db.scalars(
                select(MediaItem).where(MediaItem.path == pathmap.to_local(entry.previous_path))
            ).first()
        if item is None or not entry.path:
            continue
        item.path = pathmap.to_local(entry.path)
        renamed += 1
    db.flush()
    return f"renamed {renamed} file(s)"


def _on_file_delete(hook: Any, db: Session, title: Title) -> str:
    """§6.0 says `pending`; that means "we intend to clean it" and the file is gone.

    `stale` is the word §5/§6 already use for a vanished path, and it matters most on
    `deleteReason: "upgrade"`, which fires *before* the replacement `Download`:
    `stale -> queued` is a correct sequence, whereas `pending` would leave a phantom
    to-do if the upgrade's import then failed.
    """
    pathmap = load_path_map(db, "sonarr" if hook.kind == "series" else "radarr")
    paths = [pathmap.to_local(f.path) for f in hook.deleted_files if f.path]
    if hook.file and hook.file.path:
        paths.append(pathmap.to_local(hook.file.path))

    marked = 0
    for path in paths:
        item = db.scalars(select(MediaItem).where(MediaItem.path == path)).first()
        if item is None:
            continue
        item.status = "stale"
        _orphan_backups(db, item.id)
        marked += 1
    db.flush()
    return f"marked {marked} item(s) stale"


def _on_download(hook: Any, db: Session, title: Title) -> tuple[str, str | None]:
    file = hook.file
    if file is None or not file.path:
        return "no file in the payload", None

    pathmap = load_path_map(db, "sonarr" if hook.kind == "series" else "radarr")
    local_path = pathmap.to_local(file.path)
    episodes = (
        sorted(hook.episodes, key=lambda e: (e.season_number, e.episode_number))
        if hook.kind == "series"
        else []
    )
    first = episodes[0] if episodes else None

    # The same resolve/adopt/merge helper the sync uses, so a webhook can never
    # create a row the sync would then have to merge.
    item = resolve_item(
        db,
        title,
        local_path=local_path,
        kind="episode" if hook.kind == "series" else "movie",
        season=first.season_number if first else None,
        episode=first.episode_number if first else None,
        episode_title=" + ".join(e.title for e in episodes if e.title) or None,
        arr_file_id=file.id,
        size=file.size,
        report=SyncReport(),
    )
    if item.status == "stale":
        item.status = "pending"

    if not title.enabled:
        # §12: "disabled-title events recorded but not queued". The row is still
        # upserted, so §9.2's "12/24 clean" is right the moment the user enables it.
        db.flush()
        return "title_disabled", None

    superseded: list[str] = []
    if hook.is_upgrade:
        # §6.0: the running job is cleaning a file that no longer exists.
        for old in [pathmap.to_local(f.path) for f in hook.deleted_files if f.path]:
            replaced = db.scalars(select(MediaItem).where(MediaItem.path == old)).first()
            if replaced is not None:
                _orphan_backups(db, replaced.id)
        _orphan_backups(db, item.id)
        for job in db.scalars(
            select(Job).where(Job.media_item_id == item.id, Job.state.notin_(TERMINAL_STATES))
        ).all():
            if cancel(db, job.id, reason="superseded by upgrade"):
                superseded.append(job.id)

    result = enqueue(
        db,
        media_item_id=item.id,
        trigger="webhook",
        dedupe_window_s=DEDUPE_WINDOW_S,
    )
    note = result.reason
    if superseded:
        note = f"superseded {len(superseded)} job(s)"
    return note, result.job_id


def _orphan_backups(db: Session, media_item_id: int) -> None:
    """§13: "old backup marked orphaned and purged per retention"."""
    settings = load_settings(db)
    purge_after = (
        utcnow() + timedelta(days=settings.backup_retention_days)
        if settings.backup_retention_days
        else None
    )
    for backup in db.scalars(
        select(Backup).where(Backup.media_item_id == media_item_id, Backup.state == "kept")
    ):
        backup.state = "orphaned"
        backup.purge_after = purge_after
