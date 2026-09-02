"""Integration endpoints whose shape is already fully determined.

M3 ships only the endpoints with no UI-shaped contract left to design -- a
``TestResult``, a flat list of path mappings, and "sync now". The listing, filtering
and pagination surfaces (`library`, `items`, `jobs`) are M4's, because they should be
designed around the screens rather than inherited from whatever was convenient here.

Handlers are sync ``def``: FastAPI runs them in a threadpool, so a blocking HTTP call
to Sonarr costs a worker thread rather than the event loop, and the whole stack stays
one (synchronous) session model.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.constants import APPS
from vidcleaner.db.models import PathMapping
from vidcleaner.db.session import get_db
from vidcleaner.integrations import from_database
from vidcleaner.integrations.base import IntegrationError, IntegrationNotConfigured
from vidcleaner.integrations.pathmap import PathMap, PathRule
from vidcleaner.settings_store import MASK, load_settings

router = APIRouter(tags=["integrations"])
DbSession = Annotated[Session, Depends(get_db)]

CLIENTS = {
    "sonarr": ("sonarr_url", "sonarr_api_key"),
    "radarr": ("radarr_url", "radarr_api_key"),
    "jellyfin": ("jellyfin_url", "jellyfin_api_key"),
}


class TestRequest(BaseModel):
    """Optional overrides, so the user can test *before* saving.

    A body value of ``***`` falls through to the stored key, mirroring
    ``save_settings``: the form may never have seen the real value.
    """

    url: str | None = None
    api_key: str | None = None


class TestResponse(BaseModel):
    app: str
    ok: bool
    version: str | None = None
    detail: str = ""
    latency_ms: int = 0


class PathMappingIn(BaseModel):
    app: str
    from_prefix: str = Field(description="the app's path")
    to_prefix: str = Field(description="ours")


@router.post("/integrations/{app}/test", response_model=TestResponse)
def test_integration(
    app: str, db: DbSession, body: Annotated[TestRequest | None, Body()] = None
) -> TestResponse:
    """Never 500s: an unreachable service is a red row with a reason."""
    if app not in CLIENTS:
        raise HTTPException(status_code=404, detail=f"unknown integration {app!r}")

    settings = load_settings(db)
    url_field, key_field = CLIENTS[app]
    url = (body.url if body and body.url else None) or getattr(settings, url_field)
    key = (body.api_key if body and body.api_key and body.api_key != MASK else None) or getattr(
        settings, key_field
    )

    from vidcleaner.integrations import JellyfinClient, RadarrClient, SonarrClient

    cls = {"sonarr": SonarrClient, "radarr": RadarrClient, "jellyfin": JellyfinClient}[app]
    try:
        client = cls(url, key)
    except IntegrationNotConfigured as exc:
        return TestResponse(app=app, ok=False, detail=exc.message)
    try:
        result = client.test()
    except IntegrationError as exc:  # pragma: no cover - test() already catches these
        return TestResponse(app=app, ok=False, detail=exc.message)
    finally:
        client.close()
    return TestResponse(
        app=app,
        ok=result.ok,
        version=result.version,
        detail=result.detail,
        latency_ms=result.latency_ms,
    )


@router.get("/path-mappings", response_model=list[PathMappingIn])
def read_path_mappings(db: DbSession) -> list[PathMappingIn]:
    rows = db.scalars(select(PathMapping).order_by(PathMapping.app, PathMapping.id)).all()
    return [
        PathMappingIn(app=r.app, from_prefix=r.from_prefix, to_prefix=r.to_prefix) for r in rows
    ]


@router.put("/path-mappings", response_model=list[PathMappingIn])
def replace_path_mappings(
    mappings: Annotated[list[PathMappingIn], Body()], db: DbSession
) -> list[PathMappingIn]:
    """Replace the whole table. Validated per app before anything is written, so a
    duplicate prefix cannot leave half a mapping in place."""
    unknown = sorted({m.app for m in mappings} - set(APPS))
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown apps: {unknown}")
    for app in APPS:
        rules = [PathRule(m.from_prefix, m.to_prefix) for m in mappings if m.app == app]
        try:
            PathMap.from_rules(app, rules)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    for row in db.scalars(select(PathMapping)).all():
        db.delete(row)
    db.flush()
    for mapping in mappings:
        if mapping.from_prefix and mapping.to_prefix:
            db.add(
                PathMapping(
                    app=mapping.app,
                    from_prefix=mapping.from_prefix.rstrip("/\\"),
                    to_prefix=mapping.to_prefix.rstrip("/\\"),
                )
            )
    db.flush()
    return read_path_mappings(db)


@router.post("/library/sync")
def sync_library(db: DbSession, enqueue: bool = True, confirm: bool = False) -> dict[str, Any]:
    """§9.2's "Sync now". The same function the hourly pass calls."""
    from vidcleaner.integrations.sync import sync_all

    bundle = from_database(db)
    if bundle.sonarr is None and bundle.radarr is None:
        bundle.close()
        raise HTTPException(status_code=409, detail="neither Sonarr nor Radarr is configured")
    settings = load_settings(db)
    try:
        report = sync_all(
            integrations=bundle,
            settings=settings,
            enqueue_backfill=enqueue,
            confirm=confirm,
        )
    finally:
        bundle.close()
    return vars(report)
