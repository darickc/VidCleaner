"""FastAPI application factory. Serves the API under /api and the built SPA elsewhere."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from vidcleaner import __version__
from vidcleaner.api import health, integrations, items, jobs, library, media, webhooks
from vidcleaner.api import settings as settings_api
from vidcleaner.config import get_settings
from vidcleaner.db.migrate import upgrade_to_head
from vidcleaner.logging import configure_logging, get_logger
from vidcleaner.matching.profile import ensure_seed_data

MISSING_SPA_HTML = """<!doctype html>
<html><head><title>VidCleaner</title></head>
<body style="font-family: system-ui; margin: 3rem; max-width: 40rem">
<h1>VidCleaner API is running</h1>
<p>The web UI has not been built. From the repo root run:</p>
<pre>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>or use the Vite dev server (<code>npm run dev</code>), which proxies to this API.</p>
<p><a href="/api/health">/api/health</a> &middot; <a href="/api/docs">API docs</a></p>
</body></html>
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, role=settings.role)
    log = get_logger(__name__)
    settings.ensure_dirs()
    if settings.auto_migrate:
        upgrade_to_head(settings)
        ensure_seed_data()
    log.info(
        "api.startup",
        version=__version__,
        config_dir=str(settings.config_dir),
        static_dir=str(settings.static_dir),
        spa_built=(settings.static_dir / "index.html").is_file(),
    )
    # §8's hourly sync is owned by this process: it is HTTP-and-database only, and a
    # timer in the worker would fire however late the current ffmpeg or STT stage
    # happens to be. See the Decision Log for the split.
    syncer = _start_sync_task(settings) if settings.runs_api else None
    try:
        yield
    finally:
        if syncer is not None:
            syncer.cancel()
            with suppress(asyncio.CancelledError):
                await syncer
    log.info("api.shutdown")


def _start_sync_task(settings) -> asyncio.Task:
    """Periodic arr sync, in a thread so the blocking client never sees the loop."""
    log_ = get_logger(__name__)

    async def loop() -> None:
        interval = max(60.0, settings.sync_interval_minutes * 60.0)
        # Wait first: a container restart should not stampede the arrs, and nothing
        # depends on a sync having happened by the time the API answers.
        while True:
            await asyncio.sleep(interval)
            try:
                report = await asyncio.to_thread(_sync_once)
            except Exception:  # noqa: BLE001 - a periodic task must never die
                log_.exception("api.sync_failed")
                continue
            if report is not None:
                log_.info(
                    "api.sync_done",
                    titles=report.titles_seen,
                    items=report.items_seen,
                    enqueued=len(report.enqueued),
                    errors=len(report.errors),
                )

    return asyncio.get_running_loop().create_task(loop(), name="arr-sync")


def _sync_once():
    from vidcleaner.db.session import session_scope
    from vidcleaner.integrations import from_database
    from vidcleaner.integrations.sync import sync_all
    from vidcleaner.settings_store import load_settings

    with session_scope() as session:
        bundle = from_database(session)
        app_settings = load_settings(session)
    if bundle.sonarr is None and bundle.radarr is None:
        bundle.close()
        return None
    try:
        return sync_all(
            integrations=bundle,
            settings=app_settings,
            enqueue_backfill=True,
            confirm=True,
            mapping_check_delay_s=app_settings.mapping_check_delay_s,
        )
    finally:
        bundle.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="VidCleaner",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(library.router, prefix="/api")
    app.include_router(items.router, prefix="/api")
    app.include_router(media.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(integrations.router, prefix="/api")
    app.include_router(webhooks.router, prefix="/api")

    _mount_spa(app, settings.static_dir)
    return app


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    """Serve built assets, falling back to index.html so client-side routes deep-link."""
    index = static_dir / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    def spa(full_path: str) -> FileResponse | HTMLResponse:
        if full_path.startswith("api/"):
            # Don't answer unknown API paths with the SPA shell.
            raise HTTPException(status_code=404, detail="Not found")
        if not index.is_file():
            return HTMLResponse(MISSING_SPA_HTML, status_code=200)
        if full_path:
            candidate = (static_dir / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
                return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
