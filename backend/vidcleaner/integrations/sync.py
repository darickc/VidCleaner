"""Pulling titles and files from the arrs, and enqueueing what needs cleaning (§8).

Three jobs:

* **Titles.** Upsert every series/movie into `titles`. Never writes `enabled` or
  `profile_id` -- those are the user's, and a half-broken arr returning an empty list
  must not erase their selections. A title that disappears from the listing is
  *counted*, not deleted; only a `SeriesDelete`/`MovieDelete` webhook disables one.
* **Items, with adoption.** This is the reconciliation M1's Decision Log says M3
  owes. Resolution order is natural key, then **path** -- the second catches the rows
  a CLI run created under the sentinel title, and re-parents them while keeping
  `status`, `last_job_id`, `cleaned_at` and `source_fingerprint`. Preserving
  `last_job_id` is the entire point: `detections.media_item_id` points at that row, so
  adopting rather than inserting is what keeps an earlier run's detections attached to
  the episode M4 will show.
* **Backfill.** Enqueue anything not clean for its current profile hash, evaluated
  with **no file I/O**. Over-enqueueing is deliberately cheap: `probe` re-reads the
  `VIDCLEANER_PROFILE_HASH` tag and returns `already_clean` in about 0.1 s (measured
  in the M1 demo). The queue is the cheap filter; the tag is the definitive one.

Two things to keep in mind while reading:

* **The M1 sentinel title must be excluded from every query here** (`arr_id >= 0`).
  Nothing in §8 says so, and forgetting it makes the CLI's own scratch files show up
  as a Radarr movie called "Local files (CLI)" -- and eligible for backfill.
* **Enqueueing is chunked.** Any transaction that holds SQLite's write lock past the
  5 s busy timeout makes the worker's `BEGIN IMMEDIATE` claim fail with "database is
  locked", so a large library's backfill commits in batches rather than one go.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from vidcleaner.db.models import Backup, Detection, Job, MediaItem, Title
from vidcleaner.db.session import session_scope, utcnow
from vidcleaner.integrations.models import Episode, EpisodeFile, poster_url
from vidcleaner.integrations.pathmap import PathMap
from vidcleaner.logging import get_logger
from vidcleaner.matching.profile import matcher_for
from vidcleaner.settings_store import AppSettings, load_settings
from vidcleaner.worker.claim import enqueue

__all__ = [
    "CHUNK_SIZE",
    "MappingCheck",
    "SyncReport",
    "backfill_title",
    "confirm_mapping",
    "resolve_item",
    "sync_all",
    "sync_titles",
]

log = get_logger(__name__)

#: Rows per transaction when enqueueing. See the module docstring.
CHUNK_SIZE = 200
#: Statuses that mean "we already did this file"; everything else is a candidate.
CLEAN_STATUSES = ("clean", "already_clean")


@dataclass
class SyncReport:
    titles_seen: int = 0
    titles_added: int = 0
    titles_updated: int = 0
    titles_missing: int = 0
    items_seen: int = 0
    items_added: int = 0
    items_adopted: int = 0
    items_merged: int = 0
    items_updated: int = 0
    items_stale: int = 0
    enqueued: list[str] = field(default_factory=list)
    mapping_checked: int = 0
    mapping_mismatched: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: SyncReport) -> SyncReport:
        for name, value in vars(other).items():
            current = getattr(self, name)
            if isinstance(current, list):
                current.extend(value)
            else:
                setattr(self, name, current + value)
        return self


@dataclass(frozen=True, slots=True)
class MappingCheck:
    media_item_id: int
    ok: bool
    detail: str = ""


# ------------------------------------------------------------------- titles


def sync_titles(
    session: Session, *, client: Any, kind: str, pathmap: PathMap | None = None
) -> SyncReport:
    """Upsert every series or movie. User state (`enabled`, `profile_id`) is never
    written here.

    ``arr_path`` is stored **mapped to a local path**, like every other path in the
    database. Mapping it in a second pass over the table would double-apply on the
    next sync, since the stored value is already local by then.
    """
    pathmap = pathmap or PathMap(getattr(client, "app", kind))
    report = SyncReport()
    rows = client.list_series() if kind == "series" else client.list_movies()
    seen: set[int] = set()

    for remote in rows:
        seen.add(remote.id)
        report.titles_seen += 1
        title = session.scalars(
            select(Title).where(Title.kind == kind, Title.arr_id == remote.id)
        ).first()
        if title is None:
            title = Title(kind=kind, arr_id=remote.id, title=remote.title, enabled=False)
            session.add(title)
            report.titles_added += 1
        else:
            report.titles_updated += 1

        title.title = remote.title or title.title
        title.year = remote.year
        title.poster_url = poster_url(remote.images) or title.poster_url
        title.imdb_id = remote.imdb_id or title.imdb_id
        if kind == "series":
            title.tvdb_id = remote.tvdb_id or title.tvdb_id
            remote_path = remote.path
        else:
            title.tmdb_id = remote.tmdb_id or title.tmdb_id
            remote_path = remote.library_path
        if remote_path:
            title.arr_path = pathmap.to_local(remote_path)
        title.last_synced_at = utcnow()

    # A title we know about that the arr no longer lists is *counted*, not deleted:
    # an arr returning an empty list (restarting, half-migrated) would otherwise
    # erase every selection the user has made. Only a *Delete webhook disables one.
    known = session.scalars(select(Title.arr_id).where(Title.kind == kind, Title.arr_id >= 0)).all()
    report.titles_missing = len([arr_id for arr_id in known if arr_id not in seen])

    session.flush()
    return report


# -------------------------------------------------------------------- items


def resolve_item(
    session: Session,
    title: Title,
    *,
    local_path: str,
    kind: str,
    season: int | None,
    episode: int | None,
    episode_title: str | None,
    arr_file_id: int | None,
    size: int | None,
    report: SyncReport,
) -> MediaItem:
    """Find, adopt, merge or create the row for one arr file.

    The natural key comes first because `uq_media_items_title_s_e` enforces it. The
    path lookup comes second and is what performs **adoption**.
    """
    by_key: MediaItem | None = None
    if kind == "episode":
        by_key = session.scalars(
            select(MediaItem).where(
                MediaItem.title_id == title.id,
                MediaItem.season == season,
                MediaItem.episode == episode,
            )
        ).first()
    else:
        # §5's unique constraint does not constrain movies: SQLite treats NULLs as
        # distinct, so `(title_id, NULL, NULL)` can repeat indefinitely and repeated
        # syncs would accumulate duplicates. Guarded here instead.
        by_key = session.scalars(
            select(MediaItem).where(MediaItem.title_id == title.id).order_by(MediaItem.id)
        ).first()

    by_path = session.scalars(select(MediaItem).where(MediaItem.path == local_path)).first()

    if by_key is not None and by_path is not None and by_key.id != by_path.id:
        _merge(session, winner=by_key, loser=by_path)
        report.items_merged += 1
        item = by_key
    elif by_key is not None:
        item = by_key
    elif by_path is not None:
        item = by_path
        if item.title_id != title.id:
            # Adoption: keep everything that points at history.
            report.items_adopted += 1
            log.info(
                "sync.adopted",
                media_item_id=item.id,
                from_title=item.title_id,
                to_title=title.id,
                path=local_path,
            )
        item.title_id = title.id
    else:
        item = MediaItem(title_id=title.id, kind=kind, path=local_path, status="untracked")
        session.add(item)
        report.items_added += 1

    item.kind = kind
    item.season = season
    item.episode = episode
    item.episode_title = episode_title or item.episode_title
    item.arr_file_id = arr_file_id
    if item.path != local_path:
        item.path = local_path
        report.items_updated += 1
    if size is not None:
        item.size = size
    session.flush()
    return item


def _merge(session: Session, *, winner: MediaItem, loser: MediaItem) -> None:
    """Both lookups hit, on different rows. The unique constraint forces one to go.

    The natural-key row wins, and everything pointing at the loser is re-pointed --
    otherwise the merge would orphan an earlier run's detections, which is the exact
    history adoption exists to preserve.
    """
    session.execute(
        update(Detection).where(Detection.media_item_id == loser.id).values(media_item_id=winner.id)
    )
    session.execute(
        update(Backup).where(Backup.media_item_id == loser.id).values(media_item_id=winner.id)
    )
    session.execute(
        update(Job).where(Job.media_item_id == loser.id).values(media_item_id=winner.id)
    )
    if not winner.last_job_id:
        winner.last_job_id = loser.last_job_id
    if not winner.cleaned_at:
        winner.cleaned_at = loser.cleaned_at
    if not winner.source_fingerprint:
        winner.source_fingerprint = loser.source_fingerprint
    if winner.status in ("untracked", "pending") and loser.status not in ("untracked",):
        winner.status = loser.status
    session.flush()
    session.delete(loser)
    session.flush()
    log.info("sync.merged", winner=winner.id, loser=loser.id)


def sync_title_items(
    session: Session,
    title: Title,
    *,
    client: Any,
    pathmap: PathMap,
    report: SyncReport | None = None,
) -> SyncReport:
    """Bring one title's `media_items` in line with the arr, without enqueueing."""
    report = report or SyncReport()
    if title.arr_id is None or title.arr_id < 0:
        return report  # the CLI sentinel: never ours to sync

    live: set[int] = set()
    if title.kind == "series":
        files = {f.id: f for f in client.list_episode_files(title.arr_id)}
        for episode, file in _episode_files(client.list_episodes(title.arr_id), files):
            report.items_seen += 1
            item = resolve_item(
                session,
                title,
                local_path=pathmap.to_local(file.path),
                kind="episode",
                season=episode.season_number,
                episode=episode.episode_number,
                episode_title=episode.title,
                arr_file_id=file.id,
                size=file.size,
                report=report,
            )
            live.add(item.id)
    else:
        movie = client.get_movie(title.arr_id)
        file = movie.movie_file if movie else None
        if file is None and movie is not None and movie.has_file:
            files = client.list_movie_files(title.arr_id)
            file = files[0] if files else None
        if file is not None and file.path:
            report.items_seen += 1
            item = resolve_item(
                session,
                title,
                local_path=pathmap.to_local(file.path),
                kind="movie",
                season=None,
                episode=None,
                episode_title=None,
                arr_file_id=file.id,
                size=file.size,
                report=report,
            )
            live.add(item.id)

    # Rows whose file the arr no longer lists. §6.0 says `pending`; that is the wrong
    # word -- `pending` means "we intend to clean it" and the file is gone. `stale` is
    # what §5/§6 already use for a vanished path.
    for item in session.scalars(select(MediaItem).where(MediaItem.title_id == title.id)):
        if item.id in live or item.status == "stale":
            continue
        item.status = "stale"
        report.items_stale += 1
        for backup in session.scalars(
            select(Backup).where(Backup.media_item_id == item.id, Backup.state == "kept")
        ):
            backup.state = "orphaned"

    session.flush()
    return report


def _episode_files(
    episodes: list[Episode], files: dict[int, EpisodeFile]
) -> list[tuple[Episode, EpisodeFile]]:
    """One row per *file*, keyed on the lowest episode that shares it.

    §5 cannot represent a multi-episode file: one `episodeFile` maps to several
    `episodes[]` under scalar `season`/`episode`. Keying on the lowest episode keeps
    the natural key stable across syncs and treats `arr_file_id` as the real identity.
    A join table is an M5 item.
    """
    grouped: dict[int, list[Episode]] = {}
    for episode in episodes:
        if episode.episode_file_id and episode.episode_file_id in files:
            grouped.setdefault(episode.episode_file_id, []).append(episode)
    out = []
    for file_id, group in grouped.items():
        group.sort(key=lambda e: (e.season_number, e.episode_number))
        out.append((group[0], files[file_id]))
    return out


# ------------------------------------------------------------------ backfill


def backfill_title(
    session: Session,
    title: Title,
    *,
    client: Any,
    pathmap: PathMap,
    settings: AppSettings | None = None,
) -> list[str]:
    """Enqueue every item of an enabled title that is not clean for its own hash."""
    if not title.enabled or title.arr_id is None or title.arr_id < 0:
        return []
    settings = settings or load_settings(session)
    items = session.scalars(select(MediaItem).where(MediaItem.title_id == title.id)).all()

    queued: list[str] = []
    for item in items:
        if item.status == "stale":
            continue
        if not _needs_cleaning(session, title, item, settings=settings):
            continue
        result = enqueue(session, media_item_id=item.id, trigger="backfill")
        if result.created:
            queued.append(result.job_id)
    session.flush()
    return queued


def _needs_cleaning(
    session: Session, title: Title, item: MediaItem, *, settings: AppSettings
) -> bool:
    """Cheap checks first, and no file I/O at any point."""
    if item.status not in CLEAN_STATUSES:
        return True
    matcher = matcher_for(
        session,
        title_id=title.id,
        item_id=item.id,
        profile_id=title.profile_id,
        settings=settings,
    )
    if not item.last_job_id:
        return True
    job = session.get(Job, item.last_job_id)
    if job is None or not job.profile_snapshot_json:
        return True
    import json  # noqa: PLC0415

    try:
        recorded = json.loads(job.profile_snapshot_json).get("profile_hash")
    except ValueError:
        return True
    return recorded != matcher.profile_hash


# --------------------------------------------------------- mapping confirmation


def confirm_mapping(
    session: Session, item: MediaItem, *, client: Any, pathmap: PathMap
) -> MappingCheck:
    """§6 step 9's "confirm the arr's path equals ours", 90 s after the swap.

    It cannot be a sleep inside the stage -- that idles the single worker 90 s per
    job, half an hour for a twenty-episode season pack, and is unresumable in either
    marker order. Folded in here instead, because the sync pass already fetches
    exactly this data.
    """
    if not item.arr_file_id:
        return MappingCheck(item.id, True, "no arr file id")
    getter = getattr(client, "get_episode_file", None) or client.get_movie_file
    remote = getter(item.arr_file_id)
    if remote is None:
        return MappingCheck(item.id, False, f"the arr no longer has file {item.arr_file_id}")
    mapped = pathmap.to_local(remote.path)
    if mapped != item.path:
        return MappingCheck(item.id, False, f"the arr reports {mapped}, we have {item.path}")
    if remote.size and item.size and remote.size != item.size:
        return MappingCheck(
            item.id, False, f"the arr reports {remote.size} bytes, we have {item.size}"
        )
    return MappingCheck(item.id, True)


def _due_for_mapping_check(session: Session, *, delay_s: float) -> list[MediaItem]:
    """Cleaned longer ago than the delay, and within the last day.

    The window is what replaces the sleep: `cleaned_at` survives a restart, which a
    `time.sleep` does not.
    """
    now = utcnow()
    return list(
        session.scalars(
            select(MediaItem).where(
                MediaItem.status == "clean",
                MediaItem.cleaned_at.is_not(None),
                MediaItem.cleaned_at < now - timedelta(seconds=delay_s),
                MediaItem.cleaned_at > now - timedelta(days=1),
            )
        )
    )


# ---------------------------------------------------------------------- driver


def sync_all(
    *,
    integrations: Any,
    settings: AppSettings | None = None,
    enqueue_backfill: bool = True,
    confirm: bool = False,
    mapping_check_delay_s: float = 90.0,
) -> SyncReport:
    """The hourly pass (§8), and the "Sync now" button.

    Opens its own short transactions rather than taking a `Session`: a single
    transaction spanning a large library's HTTP calls would hold SQLite's write lock
    for far longer than the worker's claim can tolerate.
    """
    report = SyncReport()

    for kind, app in (("series", "sonarr"), ("movie", "radarr")):
        client = integrations.arr(app)
        if client is None:
            continue
        pathmap = integrations.map_for(app)
        try:
            with session_scope() as session:
                report.merge(sync_titles(session, client=client, kind=kind, pathmap=pathmap))
        except Exception as exc:  # noqa: BLE001 - one broken arr must not stop the other
            report.errors.append(f"{app} title sync failed: {exc}")
            log.warning("sync.titles_failed", app=app, error=str(exc))
            continue

        with session_scope() as session:
            title_ids = list(
                session.scalars(
                    select(Title.id).where(
                        Title.kind == kind, Title.enabled.is_(True), Title.arr_id >= 0
                    )
                )
            )

        for chunk in _chunks(title_ids, CHUNK_SIZE):
            for title_id in chunk:
                try:
                    with session_scope() as session:
                        title = session.get(Title, title_id)
                        if title is None:
                            continue
                        report.merge(
                            sync_title_items(session, title, client=client, pathmap=pathmap)
                        )
                        if enqueue_backfill:
                            report.enqueued.extend(
                                backfill_title(
                                    session,
                                    title,
                                    client=client,
                                    pathmap=pathmap,
                                    settings=settings,
                                )
                            )
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"{app} title {title_id} failed: {exc}")
                    log.warning("sync.title_failed", app=app, title_id=title_id, error=str(exc))

        if confirm:
            _confirm_due(report, client=client, pathmap=pathmap, delay_s=mapping_check_delay_s)

    log.info(
        "sync.done",
        titles=report.titles_seen,
        items=report.items_seen,
        adopted=report.items_adopted,
        merged=report.items_merged,
        stale=report.items_stale,
        enqueued=len(report.enqueued),
        errors=len(report.errors),
    )
    return report


def _confirm_due(report: SyncReport, *, client: Any, pathmap: PathMap, delay_s: float) -> None:
    with session_scope() as session:
        for item in _due_for_mapping_check(session, delay_s=delay_s):
            title = session.get(Title, item.title_id)
            if title is None or title.arr_id is None or title.arr_id < 0:
                continue
            try:
                check = confirm_mapping(session, item, client=client, pathmap=pathmap)
            except Exception as exc:  # noqa: BLE001
                report.errors.append(f"mapping check for item {item.id} failed: {exc}")
                continue
            report.mapping_checked += 1
            if not check.ok:
                report.mapping_mismatched += 1
                # §6 step 9 says "warn"; the file is correct either way.
                log.warning("sync.mapping_mismatch", media_item_id=item.id, detail=check.detail)
                if item.last_job_id:
                    from vidcleaner.worker.claim import log_event  # noqa: PLC0415

                    log_event(
                        session,
                        item.last_job_id,
                        f"arr path mapping mismatch: {check.detail}",
                        level="warning",
                    )


def _chunks(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]
