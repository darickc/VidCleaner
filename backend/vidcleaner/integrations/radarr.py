"""Radarr v5 (PLAN.md §3). The shared surface is in ``base.ArrClient``."""

from __future__ import annotations

from typing import ClassVar

from vidcleaner.integrations.base import ArrClient
from vidcleaner.integrations.models import Movie, MovieFile

__all__ = ["RadarrClient"]


class RadarrClient(ArrClient):
    app: ClassVar[str] = "radarr"
    RESCAN_COMMAND: ClassVar[str] = "RescanMovie"
    RESCAN_ID_FIELD: ClassVar[str] = "movieId"

    def list_movies(self) -> list[Movie]:
        return [Movie.model_validate(row) for row in self.get(self.api("movie")) or []]

    def get_movie(self, movie_id: int) -> Movie | None:
        payload = self.get(self.api(f"movie/{movie_id}"), allow_404=True)
        return Movie.model_validate(payload) if payload else None

    def list_movie_files(self, movie_id: int) -> list[MovieFile]:
        rows = self.get(self.api("moviefile"), params={"movieId": movie_id}) or []
        return [MovieFile.model_validate(row) for row in rows]

    def get_movie_file(self, file_id: int) -> MovieFile | None:
        payload = self.get(self.api(f"moviefile/{file_id}"), allow_404=True)
        return MovieFile.model_validate(payload) if payload else None

    def rescan_movie(self, movie_id: int):
        return self.rescan(movie_id)
