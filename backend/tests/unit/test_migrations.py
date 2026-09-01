"""The Alembic baseline must produce exactly the schema the models describe."""

from __future__ import annotations

from sqlalchemy import inspect

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
