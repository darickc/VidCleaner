"""GET /api/health — used by the compose/unraid healthcheck and the UI Queue page."""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from vidcleaner import __version__
from vidcleaner.config import get_settings
from vidcleaner.db.session import get_engine

router = APIRouter(tags=["health"])


@lru_cache(maxsize=1)
def ffmpeg_info() -> dict[str, Any]:
    """ffmpeg does not change while the process lives, so probe it once."""
    binary = shutil.which("ffprobe")
    if binary is None:
        return {"present": False, "version": None, "path": None}
    try:
        result = subprocess.run(
            [binary, "-version"], capture_output=True, text=True, timeout=5, check=False
        )
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
    except (OSError, subprocess.SubprocessError):
        return {"present": False, "version": None, "path": binary}
    return {"present": True, "version": first_line.strip() or None, "path": binary}


def _disk(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"path": str(path), "exists": False, "free_bytes": None, "total_bytes": None}
    return {
        "path": str(path),
        "exists": True,
        "free_bytes": usage.free,
        "total_bytes": usage.total,
    }


def _database() -> dict[str, Any]:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - the endpoint reports, never raises
        return {"ok": False, "error": str(exc), "revision": None}
    return {"ok": True, "error": None, "revision": revision}


@router.get("/health")
def health(response: Response) -> dict[str, Any]:
    settings = get_settings()
    database = _database()
    ffmpeg = ffmpeg_info()

    if not database["ok"]:
        # 503 so Docker restarts the container; the body still explains why.
        overall = "error"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif not ffmpeg["present"]:
        # Usable for setup and browsing, but no job can render. Still HTTP 200.
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "status": overall,
        "version": __version__,
        "role": settings.role,
        "database": database,
        "ffmpeg": ffmpeg,
        "disk": {
            "config": _disk(settings.config_dir),
            "media": _disk(settings.media_dir),
            "backups": _disk(settings.backups_dir),
            "work": _disk(settings.work_dir),
        },
    }
