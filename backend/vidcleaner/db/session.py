"""Engine and session handling.

SQLite is configured per PLAN.md §4: WAL journalling so the worker can write while the
api reads, a 5 s busy timeout, and foreign keys on (off by default in SQLite).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from vidcleaner.config import Settings, get_settings


def _apply_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def build_engine(url: str) -> Engine:
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _apply_pragmas)
    return engine


_engines: dict[str, Engine] = {}


def get_engine(settings: Settings | None = None) -> Engine:
    """One engine per database URL, shared for the life of the process."""
    settings = settings or get_settings()
    url = settings.database_url
    if url not in _engines:
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        _engines[url] = build_engine(url)
    return _engines[url]


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as session:
        yield session


def reset_engine_cache() -> None:
    """Drop cached engines/sessionmaker. Used by tests that repoint the database."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    get_sessionmaker.cache_clear()
