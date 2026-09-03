"""Serving the review clips to the Item page's players (PLAN.md §9.4).

The only endpoint in the app that turns a URL into a filesystem path, so it is also
the only one that can be walked out of its directory. Two independent guards: the
route's path parameters cannot contain a separator (they are matched against the
layout ``snippets.py`` writes, not against free text), and the resolved path must
still be inside the snippet root -- which catches a symlink planted under it as well
as anything the first check missed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from vidcleaner.config import get_settings
from vidcleaner.pipeline.snippets import FILES

router = APIRouter(prefix="/media", tags=["media"])

#: Exactly what `snippets.py` names things: a job uuid and a zero-padded index.
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
INDEX = re.compile(r"^\d{1,6}$")

MEDIA_TYPES: Final = {".m4a": "audio/mp4", ".png": "image/png"}


@router.get("/snippets/{job_id}/{index}/{name}")
def read_snippet(job_id: str, index: str, name: str) -> FileResponse:
    if not JOB_ID.match(job_id) or not INDEX.match(index) or name not in FILES:
        raise HTTPException(status_code=404, detail="no such snippet")

    root = get_settings().snippets_dir
    path = (root / job_id / index / name).resolve()
    try:
        inside = path.is_relative_to(Path(root).resolve())
    except OSError:  # pragma: no cover - resolve() on a broken mount
        inside = False
    if not inside or not path.is_file():
        raise HTTPException(status_code=404, detail="no such snippet")

    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(path.suffix, "application/octet-stream"),
        # The bytes for one (job, detection) never change: the stage rewrites the
        # whole directory under a new job id rather than editing a clip in place.
        headers={"Cache-Control": "private, max-age=86400"},
    )
