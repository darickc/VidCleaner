"""The Alembic baseline must produce exactly the schema the models describe."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from vidcleaner.config import Settings
from vidcleaner.db.migrate import upgrade_to_head
from vidcleaner.db.models import Base
from vidcleaner.db.session import get_engine


def test_upgrade_head_creates_the_full_schema(settings: Settings) -> None:
    upgrade_to_head(settings)
    inspector = inspect(get_engine(settings))
    tables = set(inspector.get_table_names())

    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_every_model_table_has_its_columns(migrated: Settings) -> None:
    inspector = inspect(get_engine(migrated))
    for name, table in Base.metadata.tables.items():
        actual = {column["name"] for column in inspector.get_columns(name)}
        expected = {column.name for column in table.columns}
        assert expected == actual, f"column drift in {name}"


def test_upgrade_is_idempotent(migrated: Settings) -> None:
    upgrade_to_head(migrated)  # must not raise on an already-migrated database


def test_sqlite_pragmas_are_applied(migrated: Settings) -> None:
    with get_engine(migrated).connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000


def test_the_movie_index_constrains_arr_rows_but_not_the_cli_sentinel(
    migrated: Settings,
) -> None:
    """Migration 0003's partial unique index, and the collision it has to avoid.

    §5's `uq_media_items_title_s_e` does not constrain movies at all -- SQLite treats
    NULLs as distinct -- so repeated syncs could accumulate duplicate rows. But the
    obvious predicate (`season IS NULL AND episode IS NULL`) would break
    `vidcleaner clean`: the CLI hangs *every* local file off one sentinel title as
    exactly that shape. `arr_file_id IS NOT NULL` is what separates the two.
    """
    from sqlalchemy import text

    engine = get_engine(migrated)
    with engine.begin() as conn:
        indexes = {row[1] for row in conn.execute(text("PRAGMA index_list('media_items')")).all()}
        assert "ux_media_items_one_movie_per_title" in indexes

        conn.execute(
            text(
                "INSERT INTO titles (kind, arr_id, title, enabled) "
                "VALUES ('movie', -1, 'Local files (CLI)', 0)"
            )
        )
        title_id = conn.execute(text("SELECT id FROM titles")).scalar_one()

        # Two CLI rows: no arr_file_id, so the index does not apply to either.
        for path in ("/media/a.mkv", "/media/b.mkv"):
            conn.execute(
                text(
                    "INSERT INTO media_items (title_id, kind, path, status) "
                    "VALUES (:t, 'movie', :p, 'untracked')"
                ),
                {"t": title_id, "p": path},
            )
        assert conn.execute(text("SELECT COUNT(*) FROM media_items")).scalar_one() == 2

        # Two arr-backed movie rows under one title: refused.
        conn.execute(
            text(
                "INSERT INTO media_items (title_id, kind, path, status, arr_file_id) "
                "VALUES (:t, 'movie', '/media/real.mkv', 'pending', 77)"
            ),
            {"t": title_id},
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO media_items (title_id, kind, path, status, arr_file_id) "
                    "VALUES (:t, 'movie', '/media/dupe.mkv', 'pending', 78)"
                ),
                {"t": title_id},
            )


def test_the_whitelist_mode_defaults_to_suppress(migrated: Settings) -> None:
    """An existing row upgraded from 0002 must keep meaning what it meant."""
    from sqlalchemy import text

    with get_engine(migrated).begin() as conn:
        conn.execute(text("INSERT INTO whitelist (scope, canonical_word) VALUES ('global', 'god')"))
        assert conn.execute(text("SELECT mode FROM whitelist")).scalar_one() == "suppress"
