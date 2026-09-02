"""Sonarr v4 (PLAN.md §3). The shared surface is in ``base.ArrClient``."""

from __future__ import annotations

from typing import ClassVar

from vidcleaner.integrations.base import ArrClient
from vidcleaner.integrations.models import Episode, EpisodeFile, Series

__all__ = ["SonarrClient"]


class SonarrClient(ArrClient):
    app: ClassVar[str] = "sonarr"
    RESCAN_COMMAND: ClassVar[str] = "RescanSeries"
    RESCAN_ID_FIELD: ClassVar[str] = "seriesId"

    def list_series(self) -> list[Series]:
        return [Series.model_validate(row) for row in self.get(self.api("series")) or []]

    def get_series(self, series_id: int) -> Series | None:
        payload = self.get(self.api(f"series/{series_id}"), allow_404=True)
        return Series.model_validate(payload) if payload else None

    def list_episodes(self, series_id: int) -> list[Episode]:
        rows = self.get(self.api("episode"), params={"seriesId": series_id}) or []
        return [Episode.model_validate(row) for row in rows]

    def list_episode_files(self, series_id: int) -> list[EpisodeFile]:
        rows = self.get(self.api("episodefile"), params={"seriesId": series_id}) or []
        return [EpisodeFile.model_validate(row) for row in rows]

    def get_episode_file(self, file_id: int) -> EpisodeFile | None:
        """``None`` on 404 rather than an exception: §6's "path vanished" path calls
        this precisely because the file may be gone."""
        payload = self.get(self.api(f"episodefile/{file_id}"), allow_404=True)
        return EpisodeFile.model_validate(payload) if payload else None

    def rescan_series(self, series_id: int):
        return self.rescan(series_id)
