"""Sonarr/Radarr webhook bodies (PLAN.md §3's payload notes).

``extra="ignore"`` with a camelCase alias generator, so the wire stays what the arrs
send and the code reads in snake_case. Every ``path`` is the **app's** path; the
receiver maps it before anything reaches the database.

Two asymmetries §3 lists but which are easy to smooth over by accident:

* Sonarr says **`SeriesAdd`** and Radarr says **`MovieAdded`**.
* Import and upgrade are both `Download`; an upgrade carries `isUpgrade: true` and
  `deletedFiles[]`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

__all__ = [
    "ArrDeletedFile",
    "ArrEpisode",
    "ArrEpisodeFile",
    "ArrMovie",
    "ArrMovieFile",
    "ArrSeries",
    "RadarrWebhook",
    "RenamedFile",
    "SonarrWebhook",
    "parse_webhook",
]

_CONFIG = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)


class _Base(BaseModel):
    model_config = _CONFIG


class ArrSeries(_Base):
    id: int
    title: str = ""
    path: str | None = None
    tvdb_id: int | None = None
    imdb_id: str | None = None
    year: int | None = None


class ArrEpisode(_Base):
    id: int | None = None
    season_number: int = 0
    episode_number: int = 0
    title: str = ""


class ArrEpisodeFile(_Base):
    id: int | None = None
    relative_path: str = ""
    path: str = ""
    size: int | None = None
    quality: str | None = None
    media_info: dict[str, Any] | None = None


class ArrMovie(_Base):
    id: int
    title: str = ""
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    folder_path: str | None = None


class ArrMovieFile(_Base):
    id: int | None = None
    relative_path: str = ""
    path: str = ""
    size: int | None = None


class ArrDeletedFile(_Base):
    id: int | None = None
    path: str = ""
    relative_path: str = ""
    size: int | None = None


class RenamedFile(_Base):
    id: int | None = None
    path: str = ""
    previous_path: str = ""


class SonarrWebhook(_Base):
    event_type: str = ""
    instance_name: str | None = None
    series: ArrSeries | None = None
    episodes: list[ArrEpisode] = []
    episode_file: ArrEpisodeFile | None = None
    deleted_files: list[ArrDeletedFile] = []
    renamed_episode_files: list[RenamedFile] = []
    is_upgrade: bool = False
    delete_reason: str | None = None

    @property
    def kind(self) -> str:
        return "series"

    @property
    def arr_id(self) -> int | None:
        return self.series.id if self.series else None

    @property
    def file(self) -> ArrEpisodeFile | None:
        return self.episode_file

    @property
    def renamed(self) -> list[RenamedFile]:
        return self.renamed_episode_files


class RadarrWebhook(_Base):
    event_type: str = ""
    instance_name: str | None = None
    movie: ArrMovie | None = None
    movie_file: ArrMovieFile | None = None
    deleted_files: list[ArrDeletedFile] = []
    renamed_movie_files: list[RenamedFile] = []
    is_upgrade: bool = False
    delete_reason: str | None = None

    @property
    def kind(self) -> str:
        return "movie"

    @property
    def arr_id(self) -> int | None:
        return self.movie.id if self.movie else None

    @property
    def file(self) -> ArrMovieFile | None:
        return self.movie_file

    @property
    def renamed(self) -> list[RenamedFile]:
        return self.renamed_movie_files


def parse_webhook(source: str, payload: dict[str, Any]) -> SonarrWebhook | RadarrWebhook:
    return (SonarrWebhook if source == "sonarr" else RadarrWebhook).model_validate(payload)
