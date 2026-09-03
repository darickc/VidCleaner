"""A small library in the database, for the API tests.

Builds the rows the M4 screens read -- a series with episodes, a movie, jobs in
several states, detections, a backup -- without running a pipeline. Every helper
returns ids rather than ORM objects: the session closes with the ``with`` block and a
detached instance is a trap the API tests do not need to know about.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from vidcleaner.db.models import Backup, Detection, Job, JobLog, MediaItem, Title
from vidcleaner.db.session import session_scope, utcnow

__all__ = ["add_backup", "add_detections", "add_job", "make_movie", "make_series"]


def make_series(
    name: str = "Show",
    *,
    arr_id: int = 1,
    episodes: int = 2,
    enabled: bool = True,
    status: str = "pending",
) -> tuple[int, list[int]]:
    with session_scope() as session:
        title = Title(
            kind="series",
            arr_id=arr_id,
            tvdb_id=1000 + arr_id,
            title=name,
            year=2020,
            enabled=enabled,
            arr_path=f"/media/tv/{name}",
        )
        session.add(title)
        session.flush()
        ids = []
        for number in range(1, episodes + 1):
            item = MediaItem(
                title_id=title.id,
                kind="episode",
                arr_file_id=100 * arr_id + number,
                season=1,
                episode=number,
                episode_title=f"Episode {number}",
                path=f"/media/tv/{name}/S01E{number:02d}.mkv",
                size=1_000_000,
                duration=1500.0,
                status=status,
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
        return title.id, ids


def make_movie(
    name: str = "Film",
    *,
    arr_id: int = 1,
    enabled: bool = True,
    status: str = "pending",
) -> tuple[int, int]:
    with session_scope() as session:
        title = Title(
            kind="movie",
            arr_id=arr_id,
            tmdb_id=2000 + arr_id,
            title=name,
            year=1999,
            enabled=enabled,
            arr_path=f"/media/movies/{name}",
        )
        session.add(title)
        session.flush()
        item = MediaItem(
            title_id=title.id,
            kind="movie",
            arr_file_id=arr_id,
            path=f"/media/movies/{name}/{name}.mkv",
            size=4_000_000,
            duration=7200.0,
            status=status,
        )
        session.add(item)
        session.flush()
        return title.id, item.id


def add_job(
    media_item_id: int,
    *,
    state: str = "queued",
    stage: str | None = None,
    trigger: str = "manual",
    priority: int = 50,
    is_last: bool = False,
    age_s: float = 0.0,
    logs: tuple[str, ...] = (),
    **columns,
) -> str:
    now = utcnow()
    with session_scope() as session:
        job = Job(
            id=str(uuid.uuid4()),
            media_item_id=media_item_id,
            trigger=trigger,
            priority=priority,
            state=state,
            stage=stage,
            created_at=now - timedelta(seconds=age_s),
            **columns,
        )
        session.add(job)
        session.flush()
        for offset, message in enumerate(logs):
            session.add(
                JobLog(job_id=job.id, ts=now + timedelta(seconds=offset), level="info", msg=message)
            )
        if is_last:
            session.get(MediaItem, media_item_id).last_job_id = job.id
        return job.id


def add_detections(job_id: str, media_item_id: int, *words: str, **overrides) -> list[int]:
    ids = []
    with session_scope() as session:
        for index, word in enumerate(words):
            row = Detection(
                job_id=job_id,
                media_item_id=media_item_id,
                word_raw=word,
                word_canonical=word,
                category=overrides.get("category", "strong"),
                start_s=10.0 * (index + 1),
                end_s=10.0 * (index + 1) + 0.4,
                mute_start_s=10.0 * (index + 1) - 0.08,
                mute_end_s=10.0 * (index + 1) + 0.52,
                source=overrides.get("source", "both"),
                confidence=0.9,
                muted=overrides.get("muted", True),
                whitelisted=overrides.get("whitelisted", False),
                suspicious=overrides.get("suspicious", False),
                snippet_path=overrides.get("snippet_path", f"{job_id}/{index:04d}"),
            )
            session.add(row)
            session.flush()
            ids.append(row.id)
    return ids


def add_backup(media_item_id: int, *, job_id: str | None = None, state: str = "kept") -> int:
    with session_scope() as session:
        item = session.get(MediaItem, media_item_id)
        backup = Backup(
            job_id=job_id,
            media_item_id=media_item_id,
            original_path=item.path,
            backup_path=f"/backups{item.path}",
            size=item.size,
            state=state,
        )
        session.add(backup)
        session.flush()
        return backup.id
