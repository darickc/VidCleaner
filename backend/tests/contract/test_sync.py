"""Titles, item adoption, the merge, and the backfill gate (PLAN.md §8).

The adoption tests are the point of this file: M1's Decision Log records that "M3
owes a reconciliation step -- when a real arr file matches a local row's path it must
adopt that row rather than inserting a duplicate", and `detections.media_item_id`
points at that row, so getting it wrong silently orphans an earlier run's work.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tests.support import fake_arr
from vidcleaner.config import Settings
from vidcleaner.db.models import Backup, Detection, Job, MediaItem, Profile, Title, WhitelistEntry
from vidcleaner.db.session import session_scope
from vidcleaner.integrations import Integrations
from vidcleaner.integrations.pathmap import PathMap, PathRule
from vidcleaner.integrations.radarr import RadarrClient
from vidcleaner.integrations.sonarr import SonarrClient
from vidcleaner.integrations.sync import (
    backfill_title,
    confirm_mapping,
    sync_all,
    sync_title_items,
    sync_titles,
)
from vidcleaner.matching.profile import clear_matcher_cache, ensure_seed_data
from vidcleaner.pipeline.persist import LOCAL_TITLE_ARR_ID, ensure_local_title

TV_MAP = PathMap.from_rules("sonarr", [PathRule("/tv", "/media/tv")])
MOVIE_MAP = PathMap.from_rules("radarr", [PathRule("/movies", "/media/movies")])
E01 = "/media/tv/Pluribus/Season 01/Pluribus - S01E01 - We is Us.mkv"


@pytest.fixture
def sonarr(migrated: Settings):
    ensure_seed_data()
    service = fake_arr.sonarr()
    return service, SonarrClient(
        "http://sonarr", "k", transport=service.transport(), sleep=lambda _: None
    )


@pytest.fixture
def radarr(migrated: Settings):
    ensure_seed_data()
    service = fake_arr.radarr()
    return service, RadarrClient(
        "http://radarr", "k", transport=service.transport(), sleep=lambda _: None
    )


def bundle(sonarr_client=None, radarr_client=None, jellyfin_client=None) -> Integrations:
    return Integrations(
        sonarr=sonarr_client,
        radarr=radarr_client,
        jellyfin=jellyfin_client,
        sonarr_map=TV_MAP,
        radarr_map=MOVIE_MAP,
    )


def enable(kind: str, arr_id: int) -> int:
    with session_scope() as session:
        title = session.scalars(
            select(Title).where(Title.kind == kind, Title.arr_id == arr_id)
        ).one()
        title.enabled = True
        return title.id


# -------------------------------------------------------------------- titles


def test_a_fresh_sync_creates_titles_disabled(sonarr) -> None:
    """§2: the user marks titles "clean"; nothing is opted in for them."""
    service, client = sonarr
    with session_scope() as session:
        report = sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    assert (report.titles_seen, report.titles_added) == (2, 2)
    with session_scope() as session:
        titles = session.scalars(select(Title).order_by(Title.arr_id)).all()
        assert [t.arr_id for t in titles] == [42, 43]
        assert all(t.enabled is False for t in titles)
        assert titles[0].tvdb_id == 445598
        assert titles[0].poster_url == "https://artworks.example/poster.jpg"


def test_the_stored_title_path_is_local(sonarr) -> None:
    """Every path in the database is ours; mapping happens only at the boundary."""
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    with session_scope() as session:
        assert session.scalars(select(Title).where(Title.arr_id == 42)).one().arr_path == (
            "/media/tv/Pluribus"
        )


def test_a_re_sync_does_not_map_the_path_twice(sonarr) -> None:
    service, client = sonarr
    for _ in range(2):
        with session_scope() as session:
            sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    with session_scope() as session:
        assert session.scalars(select(Title).where(Title.arr_id == 42)).one().arr_path == (
            "/media/tv/Pluribus"
        )


def test_user_state_survives_a_re_sync(sonarr) -> None:
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    with session_scope() as session:
        profile = Profile(name="Strict", is_default=False)
        session.add(profile)
        session.flush()
        title = session.scalars(select(Title).where(Title.arr_id == 42)).one()
        title.enabled = True
        title.profile_id = profile.id
        profile_id = profile.id

    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    with session_scope() as session:
        title = session.scalars(select(Title).where(Title.arr_id == 42)).one()
        assert title.enabled is True and title.profile_id == profile_id


def test_a_title_missing_from_the_arr_is_counted_not_deleted(sonarr) -> None:
    """An arr returning a short list (restarting, half-migrated) must not erase the
    user's selections. Only a *Delete webhook disables a title."""
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    service.routes[("GET", "/api/v3/series")] = [fake_arr.fixture("sonarr_series")[0]]

    with session_scope() as session:
        report = sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    assert report.titles_missing == 1
    with session_scope() as session:
        assert len(session.scalars(select(Title)).all()) == 2


def test_the_cli_sentinel_is_never_counted_as_missing(sonarr) -> None:
    """It has `arr_id = -1`, and every query here excludes negatives."""
    service, client = sonarr
    with session_scope() as session:
        ensure_local_title(session)
    with session_scope() as session:
        report = sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    assert report.titles_missing == 0


# --------------------------------------------------------------------- items


def test_items_are_created_with_local_paths(sonarr) -> None:
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    title_id = enable("series", 42)

    with session_scope() as session:
        report = sync_title_items(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        )
    assert (report.items_seen, report.items_added) == (2, 2)
    with session_scope() as session:
        items = session.scalars(select(MediaItem).order_by(MediaItem.episode)).all()
        assert [i.episode for i in items] == [1, 2]
        assert items[0].path == E01
        assert items[0].arr_file_id == 501
        assert items[0].episode_title == "We is Us"


def test_an_episode_with_no_file_yields_no_item(sonarr) -> None:
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    title_id = enable("series", 42)
    with session_scope() as session:
        sync_title_items(session, session.get(Title, title_id), client=client, pathmap=TV_MAP)
    with session_scope() as session:
        assert 3 not in [i.episode for i in session.scalars(select(MediaItem))]


def test_a_cli_row_is_adopted_and_keeps_its_history(sonarr) -> None:
    """The reconciliation M1's log says M3 owes. `detections.media_item_id` points
    at this row, so inserting a duplicate would silently orphan the earlier run."""
    service, client = sonarr
    with session_scope() as session:
        local = ensure_local_title(session)
        item = MediaItem(title_id=local.id, kind="movie", path=E01, status="clean")
        session.add(item)
        session.flush()
        job = Job(id="cli-job", media_item_id=item.id, trigger="manual", state="done")
        session.add(job)
        session.flush()
        session.add(
            Detection(
                job_id=job.id,
                media_item_id=item.id,
                word_raw="fuck",
                word_canonical="fuck",
                category="strong",
                start_s=1.0,
                end_s=1.3,
                mute_start_s=0.9,
                mute_end_s=1.4,
                source="both",
            )
        )
        item.last_job_id = job.id
        item_id = item.id

    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    title_id = enable("series", 42)
    with session_scope() as session:
        report = sync_title_items(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        )

    assert report.items_adopted == 1
    assert report.items_added == 1, "only the second episode is new"
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        assert item is not None, "the row was adopted, not replaced"
        assert item.title_id == title_id
        assert (item.kind, item.season, item.episode) == ("episode", 1, 1)
        assert item.last_job_id == "cli-job", "history preserved"
        assert item.status == "clean"
        assert session.scalars(select(Detection)).one().media_item_id == item_id
        # The sentinel title stays, with nothing hanging off it.
        sentinel = session.scalars(
            select(Title).where(Title.arr_id == LOCAL_TITLE_ARR_ID)
        ).one()
        assert sentinel.items == []


def test_a_collision_merges_and_re_points_everything(sonarr) -> None:
    """Both lookups hit, on different rows: the unique constraint forces one to go."""
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    title_id = enable("series", 42)

    with session_scope() as session:
        # A natural-key row at the wrong path...
        keyed = MediaItem(
            title_id=title_id, kind="episode", season=1, episode=1, path="/media/tv/old.mkv"
        )
        session.add(keyed)
        # ...and a path row from an earlier CLI run, with history.
        local = ensure_local_title(session)
        pathed = MediaItem(title_id=local.id, kind="movie", path=E01, status="clean")
        session.add(pathed)
        session.flush()
        job = Job(id="cli-job", media_item_id=pathed.id, trigger="manual", state="done")
        session.add(job)
        session.add(Backup(media_item_id=pathed.id, original_path=E01, backup_path="/b/x.mkv"))
        session.flush()
        pathed.last_job_id = job.id
        keyed_id, pathed_id = keyed.id, pathed.id

    with session_scope() as session:
        report = sync_title_items(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        )

    assert report.items_merged == 1
    with session_scope() as session:
        # Asserted by observable facts, not by "the loser's id is gone": SQLite
        # reuses a deleted rowid, so episode 2's brand-new row lands on it.
        at_path = session.scalars(select(MediaItem).where(MediaItem.path == E01)).all()
        assert len(at_path) == 1, "exactly one row owns the path"
        winner = at_path[0]
        assert winner.id == keyed_id, "the natural-key row won"
        assert (winner.season, winner.episode) == (1, 1)
        assert winner.last_job_id == "cli-job", "history carried over"
        assert session.get(Job, "cli-job").media_item_id == keyed_id
        assert session.scalars(select(Backup)).one().media_item_id == keyed_id
        # And nothing still hangs from the sentinel title.
        sentinel = session.scalars(select(Title).where(Title.arr_id == LOCAL_TITLE_ARR_ID)).one()
        assert sentinel.items == []
        assert pathed_id  # the id itself is not the assertion; see the comment above


def test_a_vanished_file_makes_the_item_stale_and_orphans_its_backup(sonarr) -> None:
    """§6.0 says `pending`; that means "we intend to clean it" and the file is gone."""
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    title_id = enable("series", 42)
    with session_scope() as session:
        sync_title_items(session, session.get(Title, title_id), client=client, pathmap=TV_MAP)
    with session_scope() as session:
        item = session.scalars(select(MediaItem).where(MediaItem.episode == 2)).one()
        session.add(Backup(media_item_id=item.id, original_path="x", backup_path="/b/x.mkv"))
        item_id = item.id

    service.routes[("GET", "/api/v3/episodefile")] = [
        fake_arr.fixture("sonarr_episodefiles")[0]
    ]
    with session_scope() as session:
        report = sync_title_items(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        )
    assert report.items_stale == 1
    with session_scope() as session:
        assert session.get(MediaItem, item_id).status == "stale"
        assert session.scalars(select(Backup)).one().state == "orphaned"


def test_a_movie_never_accumulates_duplicate_rows(radarr) -> None:
    """§5's unique constraint does not constrain `(title_id, NULL, NULL)`."""
    service, client = radarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="movie", pathmap=MOVIE_MAP)
    title_id = enable("movie", 7)
    for _ in range(3):
        with session_scope() as session:
            sync_title_items(
                session, session.get(Title, title_id), client=client, pathmap=MOVIE_MAP
            )
    with session_scope() as session:
        items = session.scalars(select(MediaItem).where(MediaItem.title_id == title_id)).all()
        assert len(items) == 1
        assert items[0].path == "/media/movies/Arrival (2016)/Arrival (2016) Bluray-1080p.mkv"


# ------------------------------------------------------------------ backfill


def synced(sonarr) -> tuple[fake_arr.FakeService, SonarrClient, int]:
    service, client = sonarr
    with session_scope() as session:
        sync_titles(session, client=client, kind="series", pathmap=TV_MAP)
    title_id = enable("series", 42)
    with session_scope() as session:
        sync_title_items(session, session.get(Title, title_id), client=client, pathmap=TV_MAP)
    return service, client, title_id


def test_enabling_a_title_backfills_its_files(sonarr) -> None:
    """§11's M3 demo: "enable a series -> existing episodes cleaned"."""
    service, client, title_id = synced(sonarr)
    with session_scope() as session:
        queued = backfill_title(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        )
    assert len(queued) == 2
    with session_scope() as session:
        jobs = session.scalars(select(Job)).all()
        assert {j.trigger for j in jobs} == {"backfill"}
        assert {j.priority for j in jobs} == {200}, "below webhook jobs (§8)"


def test_a_disabled_title_is_not_backfilled(sonarr) -> None:
    service, client, title_id = synced(sonarr)
    with session_scope() as session:
        session.get(Title, title_id).enabled = False
    with session_scope() as session:
        assert backfill_title(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        ) == []


def test_the_sentinel_title_is_never_backfilled(sonarr) -> None:
    service, client = sonarr
    with session_scope() as session:
        local = ensure_local_title(session)
        local.enabled = True
        session.add(MediaItem(title_id=local.id, kind="movie", path="/tmp/x.mkv"))
        session.flush()
        assert backfill_title(session, local, client=client, pathmap=TV_MAP) == []


def test_a_clean_item_at_the_current_hash_is_skipped(sonarr) -> None:
    service, client, title_id = synced(sonarr)
    _mark_clean(title_id, episode=1)
    with session_scope() as session:
        queued = backfill_title(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        )
    assert len(queued) == 1, "only the episode that is not clean"


def test_a_whitelist_edit_puts_a_clean_item_back_in_the_queue(sonarr) -> None:
    """The whole reason the profile hash is per item: "whitelist, then reprocess"
    must not be short-circuited by the tag already in the file."""
    service, client, title_id = synced(sonarr)
    item_id = _mark_clean(title_id, episode=1)

    with session_scope() as session:
        session.add(
            WhitelistEntry(scope="item", scope_id=item_id, canonical_word="god", context_text=None)
        )
    clear_matcher_cache()

    with session_scope() as session:
        queued = backfill_title(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        )
    assert len(queued) == 2, "the cleaned episode is a candidate again"


def _mark_clean(title_id: int, *, episode: int) -> int:
    from vidcleaner.matching.profile import matcher_for, snapshot_for
    from vidcleaner.settings_store import load_settings

    with session_scope() as session:
        item = session.scalars(
            select(MediaItem).where(
                MediaItem.title_id == title_id, MediaItem.episode == episode
            )
        ).one()
        settings = load_settings(session)
        matcher = matcher_for(session, title_id=title_id, item_id=item.id, settings=settings)
        job = Job(
            id=f"done-{item.id}",
            media_item_id=item.id,
            trigger="manual",
            state="done",
            profile_snapshot_json=snapshot_for(matcher, settings).model_dump_json(),
        )
        session.add(job)
        session.flush()
        item.status = "clean"
        item.last_job_id = job.id
        return item.id


def test_a_stale_item_is_never_enqueued(sonarr) -> None:
    service, client, title_id = synced(sonarr)
    with session_scope() as session:
        for item in session.scalars(select(MediaItem)):
            item.status = "stale"
    with session_scope() as session:
        assert backfill_title(
            session, session.get(Title, title_id), client=client, pathmap=TV_MAP
        ) == []


# ------------------------------------------------------------------- sync_all


def test_sync_all_walks_both_arrs(sonarr, radarr) -> None:
    service_s, client_s = sonarr
    service_r, client_r = radarr
    report = sync_all(integrations=bundle(client_s, client_r), enqueue_backfill=False)
    assert report.titles_seen == 4
    assert report.errors == []
    with session_scope() as session:
        assert {t.kind for t in session.scalars(select(Title))} >= {"series", "movie"}


def test_one_broken_arr_does_not_stop_the_other(sonarr, radarr) -> None:
    service_s, client_s = sonarr
    service_r, client_r = radarr
    service_s.fail[("GET", "/api/v3/series")] = 500
    report = sync_all(integrations=bundle(client_s, client_r), enqueue_backfill=False)
    assert len(report.errors) == 1 and "sonarr" in report.errors[0]
    with session_scope() as session:
        assert {t.kind for t in session.scalars(select(Title))} == {"movie"}


def test_an_unconfigured_arr_is_simply_skipped(sonarr) -> None:
    service, client = sonarr
    report = sync_all(integrations=bundle(client, None), enqueue_backfill=False)
    assert report.errors == [] and report.titles_seen == 2


def test_sync_all_enqueues_for_enabled_titles(sonarr) -> None:
    service, client = sonarr
    sync_all(integrations=bundle(client), enqueue_backfill=False)
    enable("series", 42)
    report = sync_all(integrations=bundle(client))
    assert len(report.enqueued) == 2


# --------------------------------------------------------- mapping confirmation


def test_a_matching_path_confirms(sonarr) -> None:
    service, client, title_id = synced(sonarr)
    with session_scope() as session:
        item = session.scalars(select(MediaItem).where(MediaItem.episode == 1)).one()
        item.size = 4573448192
        check = confirm_mapping(session, item, client=client, pathmap=TV_MAP)
    assert check.ok


def test_a_mismatched_path_warns_without_failing(sonarr) -> None:
    """§6 step 9 says "warn": the library file is correct either way."""
    service, client, title_id = synced(sonarr)
    with session_scope() as session:
        item = session.scalars(select(MediaItem).where(MediaItem.episode == 1)).one()
        item.path = "/media/tv/somewhere/else.mkv"
        check = confirm_mapping(session, item, client=client, pathmap=TV_MAP)
    assert not check.ok and "we have" in check.detail


def test_a_file_the_arr_forgot_is_a_mismatch(sonarr) -> None:
    service, client, title_id = synced(sonarr)
    with session_scope() as session:
        item = session.scalars(select(MediaItem).where(MediaItem.episode == 1)).one()
        item.arr_file_id = 4242
        check = confirm_mapping(session, item, client=client, pathmap=TV_MAP)
    assert not check.ok and "no longer has" in check.detail


def test_the_confirmation_only_looks_at_recently_cleaned_items(sonarr) -> None:
    """The `cleaned_at` window is what replaces §6's 90 s sleep, and unlike a sleep
    it survives a restart."""
    from datetime import timedelta

    from vidcleaner.db.session import utcnow

    service, client, title_id = synced(sonarr)
    with session_scope() as session:
        item = session.scalars(select(MediaItem).where(MediaItem.episode == 1)).one()
        item.status = "clean"
        item.size = 4573448192
        item.cleaned_at = utcnow() - timedelta(seconds=200)

    report = sync_all(integrations=bundle(client), enqueue_backfill=False, confirm=True)
    assert report.mapping_checked == 1 and report.mapping_mismatched == 0

    with session_scope() as session:
        item = session.scalars(select(MediaItem).where(MediaItem.episode == 1)).one()
        item.cleaned_at = utcnow()  # too recent: still inside the window
    report = sync_all(integrations=bundle(client), enqueue_backfill=False, confirm=True)
    assert report.mapping_checked == 0


def test_json_snapshots_stay_parseable(sonarr) -> None:
    """The backfill gate reads `profile_snapshot_json`; a schema slip there would
    silently re-enqueue the whole library."""
    service, client, title_id = synced(sonarr)
    item_id = _mark_clean(title_id, episode=1)
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        payload = json.loads(session.get(Job, item.last_job_id).profile_snapshot_json)
        assert payload["profile_hash"].startswith("v1:")
