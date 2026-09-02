"""Jellyfin 10.10 (PLAN.md §3).

The cheapest refresh is ``POST /Library/Media/Updated`` with the paths that changed;
§3 measured roughly a 60 s debounce, which is fine because nothing downstream waits.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from vidcleaner.integrations.base import BaseClient, IntegrationError, TestResult

__all__ = ["JellyfinClient", "MediaUpdate"]


class MediaUpdate(BaseModel):
    """One entry in ``/Library/Media/Updated``. Aliased, because Jellyfin's body is
    PascalCase and getting it wrong fails silently -- the call still returns 204."""

    model_config = ConfigDict(populate_by_name=True)

    path: str = Field(alias="Path")
    update_type: Literal["Created", "Modified", "Deleted"] = Field(
        default="Modified", alias="UpdateType"
    )


class JellyfinClient(BaseClient):
    app: ClassVar[str] = "jellyfin"
    IDEMPOTENT_POSTS: ClassVar[tuple[str, ...]] = ("/Library/",)

    def auth_headers(self) -> dict[str, str]:
        # §3's exact form. Jellyfin also accepts an X-Emby-Token header, but this is
        # the documented one for 10.10.
        return {
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
            "Accept": "application/json",
        }

    def test(self) -> TestResult:
        """``/System/Info``, not ``/System/Info/Public``: the public variant answers
        without a key, so it would report success for a wrong one."""
        started = time.monotonic()
        try:
            payload = self.get("/System/Info") or {}
        except IntegrationError as exc:
            return TestResult(False, self.app, detail=exc.message)
        return TestResult(
            True,
            self.app,
            version=str(payload.get("Version") or "") or None,
            detail=str(payload.get("ServerName") or self.app),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def media_updated(self, updates: Sequence[MediaUpdate]) -> None:
        """One call for every path that changed. A 204 is success, not an error."""
        if not updates:
            return
        self.post(
            "/Library/Media/Updated",
            json={"Updates": [u.model_dump(by_alias=True) for u in updates]},
        )

    def refresh_series(self, tvdb_id: int) -> None:
        self.post("/Library/Series/Updated", params={"tvdbId": tvdb_id})

    def refresh_movie(self, tmdb_id: int) -> None:
        self.post("/Library/Movies/Updated", params={"tmdbId": tmdb_id})
