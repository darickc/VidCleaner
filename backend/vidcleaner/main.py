"""FastAPI application factory. Serves the API under /api and the built SPA elsewhere."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from vidcleaner import __version__
from vidcleaner.api import health
from vidcleaner.api import settings as settings_api
from vidcleaner.config import get_settings
from vidcleaner.db.migrate import upgrade_to_head
from vidcleaner.logging import configure_logging, get_logger

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
    log.info(
        "api.startup",
        version=__version__,
        config_dir=str(settings.config_dir),
        static_dir=str(settings.static_dir),
        spa_built=(settings.static_dir / "index.html").is_file(),
    )
    yield
    log.info("api.shutdown")


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
    app.include_router(settings_api.router, prefix="/api")

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
