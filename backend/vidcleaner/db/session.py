"""Engine and session handling.

SQLite is configured per PLAN.md §4: WAL journalling so the worker can write while the
api reads, a 5 s busy timeout, and foreign keys on (off by default in SQLite).

Two helpers exist for the M3 queue and are documented here because both encode a
SQLite behaviour that is easy to get wrong:

* :func:`immediate_connection` -- the queue's claim reads a row and then updates it.
  Under WAL a deferred transaction cannot do that: the upgrade fails with
  ``SQLITE_BUSY_SNAPSHOT`` *immediately*, because SQLite does not invoke the busy
  handler for snapshot conflicts, so ``busy_timeout`` does not help. ``BEGIN
  IMMEDIATE`` takes the write lock up front, and ``busy_timeout`` *does* cover that.
* :func:`utcnow` -- ``DateTime(timezone=True)`` is a no-op on SQLite: an aware
  datetime round-trips as a **naive** one, so comparing it against
  ``datetime.now(UTC)`` raises ``TypeError``. Every timestamp the queue compares
  (heartbeats, ``retry_at``) goes through this function instead. Nothing has hit it
  before because the CLI never compared two timestamps.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from vidcleaner.config import Settings, get_settings


def utcnow() -> datetime:
    """Naive UTC -- the one clock for values compared against database columns."""
    return datetime.now(UTC).replace(tzinfo=None)


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


@contextmanager
def immediate_connection(
    settings: Settings | None = None, *, busy_timeout_ms: int | None = None
) -> Iterator[Connection]:
    """A SQLite write transaction that takes the write lock before reading anything.

    Deliberately *not* wired up as a global ``begin`` event: emitting ``BEGIN
    IMMEDIATE`` for every transaction would make the api's read-only queries take the
    write lock, which is exactly what WAL exists to avoid. Only the two operations
    that read-then-write (claim and enqueue) use this.

    ``busy_timeout_ms`` overrides the connection's pragma, which is only useful to
    tests that deliberately contend on the lock and should not wait 5 s to prove it.
    """
    engine = get_engine(settings)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if busy_timeout_ms is not None:
            connection.exec_driver_sql(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.exec_driver_sql("ROLLBACK")
            raise
        connection.exec_driver_sql("COMMIT")


def reset_engine_cache() -> None:
    """Drop cached engines/sessionmaker. Used by tests that repoint the database."""
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
    get_sessionmaker.cache_clear()
