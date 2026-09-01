"""GET/PATCH /api/settings — the operational settings in the ``settings`` table."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from vidcleaner.db.session import get_db
from vidcleaner.settings_store import load_settings, masked, save_settings

router = APIRouter(prefix="/settings", tags=["settings"])


DbSession = Annotated[Session, Depends(get_db)]


@router.get("")
def read_settings(db: DbSession) -> dict[str, Any]:
    return masked(load_settings(db))


@router.patch("")
def update_settings(patch: Annotated[dict[str, Any], Body()], db: DbSession) -> dict[str, Any]:
    try:
        updated = save_settings(db, patch)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors(include_url=False)
        ) from exc
    return masked(updated)
