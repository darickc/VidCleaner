"""Parsed arr API responses (PLAN.md §3's endpoint list).

``extra="ignore"`` throughout: these APIs add fields between releases and we read a
handful. Every ``path`` here is the **app's** path as reported -- callers map it to
ours through ``PathMap.to_local`` before anything reaches the database.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Episode", "EpisodeFile", "Movie", "MovieFile", "Series", "poster_url"]

_CONFIG = ConfigDict(extra="ignore", populate_by_name=True)


class Series(BaseModel):
    model_config = _CONFIG

    id: int
    title: str = ""
    year: int | None = None
    path: str | None = None
    tvdb_id: int | None = Field(default=None, alias="tvdbId")
    imdb_id: str | None = Field(default=None, alias="imdbId")
    images: list[dict[str, Any]] = Field(default_factory=list)


class Episode(BaseModel):
    model_config = _CONFIG

    id: int
    season_number: int = Field(alias="seasonNumber")
    episode_number: int = Field(alias="episodeNumber")
    title: str = ""
    episode_file_id: int = Field(default=0, alias="episodeFileId")
    has_file: bool = Field(default=False, alias="hasFile")


class EpisodeFile(BaseModel):
    model_config = _CONFIG

    id: int
    series_id: int | None = Field(default=None, alias="seriesId")
    season_number: int | None = Field(default=None, alias="seasonNumber")
    relative_path: str = Field(default="", alias="relativePath")
    path: str = ""
    size: int | None = None


class Movie(BaseModel):
    model_config = _CONFIG

    id: int
    title: str = ""
    year: int | None = None
    path: str | None = None
    folder_path: str | None = Field(default=None, alias="folderPath")
    tmdb_id: int | None = Field(default=None, alias="tmdbId")
    imdb_id: str | None = Field(default=None, alias="imdbId")
    has_file: bool = Field(default=False, alias="hasFile")
    movie_file: MovieFile | None = Field(default=None, alias="movieFile")
    images: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def library_path(self) -> str | None:
        return self.path or self.folder_path


class MovieFile(BaseModel):
    model_config = _CONFIG

    id: int
    movie_id: int | None = Field(default=None, alias="movieId")
    relative_path: str = Field(default="", alias="relativePath")
    path: str = ""
    size: int | None = None


Movie.model_rebuild()


def poster_url(images: list[dict[str, Any]]) -> str | None:
    """§9.2 shows a poster. ``remoteUrl`` is preferred: a relative ``url`` only
    resolves against the arr's own base, which the browser may not be able to reach."""
    for image in images:
        if str(image.get("coverType") or "").lower() == "poster":
            return image.get("remoteUrl") or image.get("url")
    return None
