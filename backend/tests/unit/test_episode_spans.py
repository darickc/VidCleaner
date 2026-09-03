"""Multi-episode files (PLAN.md §5's gap, closed by migration 0003).

One Sonarr ``episodeFile`` can cover several ``episodes[]``, which scalar
``season``/``episode`` columns cannot hold. M3 keyed on the lowest pair and treated
``arr_file_id`` as the real identity; these pin that this is still true and that the
join table only ever *labels*.
"""

from __future__ import annotations

from sqlalchemy import select

from tests.support.library import make_series
from vidcleaner.api.views import episode_code, episode_spans, item_label
from vidcleaner.db.models import MediaItem, MediaItemEpisode, Title
from vidcleaner.db.session import session_scope
from vidcleaner.integrations.sync import EpisodeSpan, set_episode_spans


def spans_of(item_id: int) -> list[tuple[int, int]]:
    with session_scope() as session:
        return [
            (row.season, row.episode)
            for row in session.scalars(
                select(MediaItemEpisode)
                .where(MediaItemEpisode.media_item_id == item_id)
                .order_by(MediaItemEpisode.season, MediaItemEpisode.episode)
            )
        ]


# ------------------------------------------------------------------- the writer


def test_spans_are_written_and_pruned(migrated) -> None:
    """A re-cut file that used to cover E01-E02 must stop claiming E02."""
    _title_id, episodes = make_series(episodes=1)
    item_id = episodes[0]

    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        assert (
            set_episode_spans(
                session, item, [EpisodeSpan(1, 1, "Pilot", 11), EpisodeSpan(1, 2, "Part 2", 12)]
            )
            == 2
        )
    assert spans_of(item_id) == [(1, 1), (1, 2)]

    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        assert set_episode_spans(session, item, [EpisodeSpan(1, 1, "Pilot", 11)]) == 1
    assert spans_of(item_id) == [(1, 1)]


def test_writing_the_same_spans_twice_is_idempotent(migrated) -> None:
    _title_id, episodes = make_series(episodes=1)
    spans = [EpisodeSpan(1, 1), EpisodeSpan(1, 2)]
    for _ in range(3):
        with session_scope() as session:
            set_episode_spans(session, session.get(MediaItem, episodes[0]), spans)
    assert spans_of(episodes[0]) == [(1, 1), (1, 2)]


def test_a_movie_gets_no_spans(migrated) -> None:
    """The table is episode-only; a movie has nothing to disambiguate."""
    from tests.support.library import make_movie

    _title_id, item_id = make_movie()
    with session_scope() as session:
        assert set_episode_spans(session, session.get(MediaItem, item_id), [EpisodeSpan(1, 1)]) == 0
    assert spans_of(item_id) == []


def test_spans_are_deleted_with_the_item(migrated) -> None:
    _title_id, episodes = make_series(episodes=1)
    with session_scope() as session:
        set_episode_spans(session, session.get(MediaItem, episodes[0]), [EpisodeSpan(1, 1)])
    with session_scope() as session:
        session.delete(session.get(MediaItem, episodes[0]))
    assert spans_of(episodes[0]) == []


def test_the_loader_returns_one_entry_per_item(migrated) -> None:
    _title_id, episodes = make_series(episodes=2)
    with session_scope() as session:
        set_episode_spans(
            session, session.get(MediaItem, episodes[0]), [EpisodeSpan(1, 1), EpisodeSpan(1, 2)]
        )
    with session_scope() as session:
        items = list(session.scalars(select(MediaItem).order_by(MediaItem.id)))
        loaded = episode_spans(session, items)
    assert loaded == {episodes[0]: [(1, 1), (1, 2)]}


# -------------------------------------------------------------------- the label


def test_a_single_episode_reads_as_one_code() -> None:
    item = MediaItem(kind="episode", season=1, episode=3, path="/x.mkv")
    assert episode_code(item) == "S01E03"


def test_a_contiguous_run_collapses_to_a_range() -> None:
    item = MediaItem(kind="episode", season=1, episode=1, path="/x.mkv")
    assert episode_code(item, [(1, 1), (1, 2)]) == "S01E01-E02"
    assert episode_code(item, [(1, 1), (1, 2), (1, 3)]) == "S01E01-E03"


def test_a_gap_is_listed_rather_than_ranged() -> None:
    """ "S01E01-E05" would be a lie about a file holding only E01 and E05."""
    item = MediaItem(kind="episode", season=1, episode=1, path="/x.mkv")
    assert episode_code(item, [(1, 1), (1, 5)]) == "S01E01+S01E05"


def test_a_season_boundary_is_listed_too() -> None:
    item = MediaItem(kind="episode", season=1, episode=13, path="/x.mkv")
    assert episode_code(item, [(1, 13), (2, 1)]) == "S01E13+S02E01"


def test_the_scalar_columns_are_the_fallback() -> None:
    """M3's key is still the identity; the join table is additive labelling only."""
    item = MediaItem(kind="episode", season=2, episode=7, path="/x.mkv")
    assert episode_code(item, []) == "S02E07"


def test_the_item_label_uses_the_range(migrated) -> None:
    title = Title(kind="series", arr_id=1, title="Show")
    item = MediaItem(
        kind="episode", season=1, episode=1, path="/x.mkv", episode_title="Pilot + Part 2"
    )
    assert item_label(item, title, [(1, 1), (1, 2)]) == "Show S01E01-E02 — Pilot + Part 2"
