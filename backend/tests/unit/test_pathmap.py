"""Prefix mapping between our paths and an app's (PLAN.md §2, §8).

The invariant these guard: the database stores **local** paths exclusively, so a
mapping bug shows up as a refused swap at best and a wrong file at worst.
"""

from __future__ import annotations

import pytest

from vidcleaner.config import Settings
from vidcleaner.db.models import PathMapping
from vidcleaner.db.session import session_scope
from vidcleaner.integrations.pathmap import PathMap, PathRule, load_path_map


def mapped(*pairs: tuple[str, str]) -> PathMap:
    return PathMap.from_rules("sonarr", [PathRule(a, b) for a, b in pairs])


def test_an_empty_map_is_identity() -> None:
    """§2: "Implement an optional prefix-mapping table (identity default)"."""
    empty = PathMap("sonarr")
    assert not empty
    assert empty.to_local("/tv/Show/x.mkv") == "/tv/Show/x.mkv"
    assert empty.to_remote("/media/tv/Show/x.mkv") == "/media/tv/Show/x.mkv"


def test_the_direction_is_pinned() -> None:
    """§5 never says which side is ours. `from_prefix` is the app's, `to_prefix`
    ours -- the arrs hand us paths, we hand Jellyfin paths."""
    m = mapped(("/tv", "/media/tv"))
    assert m.to_local("/tv/Show/x.mkv") == "/media/tv/Show/x.mkv"
    assert m.to_remote("/media/tv/Show/x.mkv") == "/tv/Show/x.mkv"


def test_the_longest_prefix_wins() -> None:
    m = mapped(("/data", "/media"), ("/data/tv", "/media/television"))
    assert m.to_local("/data/tv/Show/x.mkv") == "/media/television/Show/x.mkv"
    assert m.to_local("/data/movies/y.mkv") == "/media/movies/y.mkv"


def test_matching_is_component_aware() -> None:
    """A naive `startswith` makes a rule for /media/tv rewrite /media/tvshows --
    a silent wrong-path bug whose best case is a refused swap."""
    m = mapped(("/media/tv", "/library/tv"))
    assert m.to_local("/media/tvshows/Other/x.mkv") == "/media/tvshows/Other/x.mkv"
    assert m.to_local("/media/tv/Show/x.mkv") == "/library/tv/Show/x.mkv"


def test_the_prefix_itself_maps() -> None:
    m = mapped(("/tv", "/media/tv"))
    assert m.to_local("/tv") == "/media/tv"


def test_trailing_separators_are_normalised() -> None:
    m = mapped(("/tv/", "/media/tv/"))
    assert m.to_local("/tv/Show/x.mkv") == "/media/tv/Show/x.mkv"


def test_an_unmapped_path_passes_through() -> None:
    m = mapped(("/tv", "/media/tv"))
    assert m.to_local("/somewhere/else.mkv") == "/somewhere/else.mkv"


def test_the_round_trip_is_the_identity() -> None:
    m = mapped(("/tv", "/media/tv"), ("/films", "/media/movies"))
    for path in ("/tv/Show/S01E01.mkv", "/films/Arrival (2016)/a.mkv", "/other/x.mkv"):
        assert m.to_remote(m.to_local(path)) == path


def test_a_windows_arr_path_is_not_mangled() -> None:
    """`Path` on Linux would turn `C:\\media\\TV\\x.mkv` into one component."""
    m = PathMap.from_rules("sonarr", [PathRule(r"C:\media\TV", "/media/tv")])
    assert m.to_local(r"C:\media\TV\Show\x.mkv") == "/media/tv\\Show\\x.mkv"
    assert m.to_local(r"C:\media\TVShows\x.mkv") == r"C:\media\TVShows\x.mkv"


def test_a_duplicate_source_prefix_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        mapped(("/tv", "/media/tv"), ("/tv", "/media/other"))


def test_a_duplicate_destination_prefix_is_rejected() -> None:
    """§5 constrains neither, but a duplicate `to_prefix` makes `to_remote`
    ambiguous -- there would be two right answers."""
    with pytest.raises(ValueError, match="duplicate"):
        mapped(("/tv", "/media/tv"), ("/television", "/media/tv"))


def test_an_incomplete_rule_is_dropped() -> None:
    assert not mapped(("", "/media/tv"))
    assert not mapped(("/tv", ""))


def test_an_empty_path_is_returned_unchanged() -> None:
    assert mapped(("/tv", "/media/tv")).to_local("") == ""


# ------------------------------------------------------------------- database


def test_rules_load_from_the_table(migrated: Settings) -> None:
    with session_scope() as session:
        session.add(PathMapping(app="sonarr", from_prefix="/tv", to_prefix="/media/tv"))
        session.add(PathMapping(app="jellyfin", from_prefix="/data", to_prefix="/media"))
    with session_scope() as session:
        assert load_path_map(session, "sonarr").to_local("/tv/x.mkv") == "/media/tv/x.mkv"
        assert load_path_map(session, "jellyfin").to_remote("/media/x.mkv") == "/data/x.mkv"
        assert not load_path_map(session, "radarr")


def test_a_broken_mapping_table_degrades_to_identity(migrated: Settings) -> None:
    """Identity is wrong but recoverable; taking the integration down is not, and
    the Settings page can show the problem."""
    with session_scope() as session:
        session.add(PathMapping(app="sonarr", from_prefix="/tv", to_prefix="/media/tv"))
        session.add(PathMapping(app="sonarr", from_prefix="/shows", to_prefix="/media/tv"))
    with session_scope() as session:
        assert not load_path_map(session, "sonarr")
