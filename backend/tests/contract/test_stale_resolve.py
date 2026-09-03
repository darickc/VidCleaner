"""§6's "path vanished": re-resolve through the arr, requeue once, else `stale`.

`policy.classify` has implemented the requeue since M3 and `StaleSourceError`'s
docstring has described the re-resolution -- but nothing re-resolved anything, so the
one allowed retry ran against the identical path sixty seconds later and could only
fail the same way. These pin the missing half.
"""

from __future__ import annotations

import pytest

from tests.support import fake_arr
from tests.support.library import make_movie, make_series
from vidcleaner.config import Settings
from vidcleaner.db.models import MediaItem, Title
from vidcleaner.db.session import session_scope
from vidcleaner.integrations import Integrations
from vidcleaner.integrations.pathmap import PathMap, PathRule
from vidcleaner.integrations.radarr import RadarrClient
from vidcleaner.integrations.sonarr import SonarrClient
from vidcleaner.pipeline.stages import StaleSourceError
from vidcleaner.worker.policy import classify
from vidcleaner.worker.resolve import reresolve_path

TV_MAP = PathMap.from_rules("sonarr", [PathRule("/tv", "/media/tv")])
MOVIE_MAP = PathMap.from_rules("radarr", [PathRule("/movies", "/media/movies")])


@pytest.fixture
def sonarr(migrated: Settings):
    service = fake_arr.sonarr()
    return service, Integrations(
        sonarr=SonarrClient(
            "http://sonarr", "k", transport=service.transport(), sleep=lambda _: None
        ),
        sonarr_map=TV_MAP,
    )


@pytest.fixture
def radarr(migrated: Settings):
    service = fake_arr.radarr()
    return service, Integrations(
        radarr=RadarrClient(
            "http://radarr", "k", transport=service.transport(), sleep=lambda _: None
        ),
        radarr_map=MOVIE_MAP,
    )


def resolve(item_id: int, integrations):
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        title = session.get(Title, item.title_id)
        return reresolve_path(session, item, title, integrations)


def path_of(item_id: int) -> str:
    with session_scope() as session:
        return session.get(MediaItem, item_id).path


def set_file_id(item_id: int, file_id: int, *, arr_id: int = 42) -> None:
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        item.arr_file_id = file_id
        session.get(Title, item.title_id).arr_id = arr_id


# ------------------------------------------------------------------- the lookup


def test_a_moved_episode_is_found_and_the_path_is_written(sonarr) -> None:
    """Keyed on `arr_file_id`, which is the identity M3 settled on -- and which
    survives exactly the folder moves and renames that cause this failure."""
    service, integrations = sonarr
    _title_id, episodes = make_series()
    item_id = episodes[0]
    set_file_id(item_id, 501)
    with session_scope() as session:
        session.get(MediaItem, item_id).path = "/media/tv/Show/old-name.mkv"

    result = resolve(item_id, integrations)

    assert result.outcome == "moved"
    assert result.worth_retrying is True
    assert path_of(item_id) == "/media/tv/Pluribus/Season 01/Pluribus - S01E01 - We is Us.mkv"
    assert ("GET", "/api/v3/episodefile/501") in [c.where for c in service.calls]


def test_the_arr_path_is_mapped_to_ours(sonarr) -> None:
    """The arr answers in its own namespace; §2's mapping is applied at the boundary."""
    _service, integrations = sonarr
    _title_id, episodes = make_series()
    set_file_id(episodes[0], 501)
    result = resolve(episodes[0], integrations)
    assert result.path is not None
    assert result.path.startswith("/media/tv/"), "not the arr's /tv"


def test_an_unchanged_path_is_not_worth_a_retry(sonarr) -> None:
    """The point of the whole module. A retry against the same path fails for the
    same reason a minute later; all it buys is a burned attempt."""
    _service, integrations = sonarr
    _title_id, episodes = make_series()
    item_id = episodes[0]
    set_file_id(item_id, 501)
    with session_scope() as session:
        session.get(
            MediaItem, item_id
        ).path = "/media/tv/Pluribus/Season 01/Pluribus - S01E01 - We is Us.mkv"

    result = resolve(item_id, integrations)
    assert result.outcome == "unchanged"
    assert result.worth_retrying is False


def test_a_file_the_arr_has_forgotten_is_gone(sonarr) -> None:
    """404 comes back as `None` rather than an exception, precisely for this."""
    service, integrations = sonarr
    service.route("GET", "/api/v3/episodefile/501", None)
    _title_id, episodes = make_series()
    set_file_id(episodes[0], 501)

    result = resolve(episodes[0], integrations)
    assert result.outcome == "gone"
    assert result.worth_retrying is False


def test_a_moved_movie_uses_the_radarr_endpoint(radarr) -> None:
    service, integrations = radarr
    title_id, item_id = make_movie()
    with session_scope() as session:
        session.get(Title, title_id).kind = "movie"
        session.get(Title, title_id).arr_id = 7
        item = session.get(MediaItem, item_id)
        item.arr_file_id = 901
        item.path = "/media/movies/Arrival/old.mkv"

    result = resolve(item_id, integrations)
    assert result.outcome == "moved"
    assert ("GET", "/api/v3/moviefile/901") in [c.where for c in service.calls]
    assert path_of(item_id).startswith("/media/movies/Arrival (2016)/")


# ------------------------------------------------------------- nothing to ask


def test_a_cli_row_has_nothing_to_resolve(sonarr) -> None:
    """No `arr_file_id`, so there is no question to put to anyone."""
    _service, integrations = sonarr
    _title_id, episodes = make_series()
    with session_scope() as session:
        session.get(MediaItem, episodes[0]).arr_file_id = None
    result = resolve(episodes[0], integrations)
    assert result.outcome == "unavailable"
    assert "arr file id" in result.detail


def test_an_unconfigured_install_is_unavailable(migrated) -> None:
    _title_id, episodes = make_series()
    set_file_id(episodes[0], 501)
    assert resolve(episodes[0], Integrations()).outcome == "unavailable"
    assert resolve(episodes[0], None).outcome == "unavailable"


def test_a_hung_arr_does_not_mask_the_real_failure(sonarr) -> None:
    """The stage failure is the interesting news; a timeout here must not replace it."""
    service, integrations = sonarr
    service.raise_timeout.add(("GET", "/api/v3/episodefile/501"))
    _title_id, episodes = make_series()
    set_file_id(episodes[0], 501)

    result = resolve(episodes[0], integrations)
    assert result.outcome == "unavailable"


# -------------------------------------------------------- what policy does with it


def test_a_moved_file_earns_a_fast_requeue() -> None:
    verdict = classify("probe", StaleSourceError("probe", "gone"), attempts=1, resolved="moved")
    assert verdict.state == "queued"
    assert verdict.retry_in_s == 5.0, "no need to wait a minute: we know where it went"


def test_a_moved_file_is_requeued_even_after_several_claims() -> None:
    """`attempts` counts **claims**, so a crash could have spent M3's whole allowance
    before the file ever moved."""
    verdict = classify("swap", StaleSourceError("swap", "gone"), attempts=3, resolved="moved")
    assert verdict.state == "queued"


def test_a_file_the_arr_has_forgotten_goes_straight_to_stale() -> None:
    verdict = classify("probe", StaleSourceError("probe", "gone"), attempts=1, resolved="gone")
    assert verdict.state == "stale"
    assert "no longer has this file" in verdict.detail


def test_an_unchanged_path_goes_straight_to_stale() -> None:
    verdict = classify("probe", StaleSourceError("probe", "gone"), attempts=1, resolved="unchanged")
    assert verdict.state == "stale"


def test_no_arr_falls_back_to_one_blind_retry() -> None:
    """M3's behaviour, kept for a CLI row or an unconfigured install: "we do not know"
    is the same state whether nothing asked or there was nobody to ask."""
    for resolved in (None, "unavailable"):
        first = classify("probe", StaleSourceError("probe", "gone"), attempts=1, resolved=resolved)
        assert first.state == "queued", resolved
        assert first.retry_in_s == 60.0
        second = classify("probe", StaleSourceError("probe", "gone"), attempts=2, resolved=resolved)
        assert second.state == "stale", resolved
