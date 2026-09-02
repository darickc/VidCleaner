"""The schema and constants the M3 queue rests on.

Everything here guards a SQLite behaviour or a PLAN.md ambiguity that would
otherwise be discovered by a wrong mute or a stuck queue rather than by a test.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError, OperationalError

from vidcleaner.config import Settings
from vidcleaner.db.constants import (
    DEFAULT_PRIORITY,
    JOB_STAGES,
    JOB_STATES,
    JOB_TRIGGERS,
    RUNNING_STATES,
    STAGE_TO_STATE,
    TERMINAL_STATES,
)
from vidcleaner.db.models import Base, Job, MediaItem, Title
from vidcleaner.db.session import (
    get_engine,
    immediate_connection,
    session_scope,
    utcnow,
)

# ----------------------------------------------------------------- constants


def test_stage_to_state_covers_every_stage() -> None:
    assert set(STAGE_TO_STATE) == set(JOB_STAGES)
    assert set(STAGE_TO_STATE.values()) <= set(JOB_STATES)


def test_running_and_terminal_states_partition_job_states() -> None:
    assert set(RUNNING_STATES) | set(TERMINAL_STATES) | {"queued"} == set(JOB_STATES)
    assert not set(RUNNING_STATES) & set(TERMINAL_STATES)


def test_cancelled_is_a_state() -> None:
    """§6.0's upgrade supersede and §9.1's cancel button both need it."""
    assert "cancelled" in JOB_STATES
    assert "cancelled" in TERMINAL_STATES


def test_priority_direction_is_lower_runs_sooner() -> None:
    """§4 orders by priority ascending, so §8's "below webhook" is a bigger number."""
    assert set(DEFAULT_PRIORITY) >= set(JOB_TRIGGERS)
    assert DEFAULT_PRIORITY["manual"] < DEFAULT_PRIORITY["webhook"]
    assert DEFAULT_PRIORITY["webhook"] < DEFAULT_PRIORITY["backfill"]
    assert DEFAULT_PRIORITY["backfill"] < DEFAULT_PRIORITY["audit"]
    assert DEFAULT_PRIORITY["webhook"] == Job.__table__.c.priority.default.arg


# --------------------------------------------------------------------- schema


def test_migration_creates_every_model_index(migrated: Settings) -> None:
    """Column drift is already covered; index drift is how 0002 could half-land."""
    inspector = inspect(get_engine(migrated))
    for name, table in Base.metadata.tables.items():
        actual = {index["name"] for index in inspector.get_indexes(name)}
        expected = {index.name for index in table.indexes}
        assert expected <= actual, f"missing indexes on {name}"


def _item(session) -> MediaItem:
    title = Title(kind="series", arr_id=1, title="Show")
    session.add(title)
    session.flush()
    item = MediaItem(title_id=title.id, kind="episode", path="/media/tv/s01e01.mkv")
    session.add(item)
    session.flush()
    return item


def test_only_one_live_job_per_media_item(migrated: Settings) -> None:
    with session_scope() as session:
        item_id = _item(session).id
        session.add(Job(id="a", media_item_id=item_id, trigger="webhook", state="queued"))
        session.flush()
        session.add(Job(id="b", media_item_id=item_id, trigger="backfill", state="queued"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_a_terminal_job_does_not_block_a_new_one(migrated: Settings) -> None:
    for state in TERMINAL_STATES:
        with session_scope() as session:
            session.query(Job).delete()
            session.query(MediaItem).delete()
            session.query(Title).delete()
            item_id = _item(session).id
            session.add(Job(id="a", media_item_id=item_id, trigger="webhook", state=state))
            session.flush()
            session.add(Job(id="b", media_item_id=item_id, trigger="webhook", state="queued"))
            session.flush()  # must not raise


# ------------------------------------------------------- immediate_connection


def test_immediate_connection_commits(migrated: Settings) -> None:
    with immediate_connection(migrated) as connection:
        connection.execute(
            Title.__table__.insert().values(kind="movie", arr_id=7, title="Film", enabled=False)
        )
    with session_scope() as session:
        assert session.scalars(select(Title).where(Title.arr_id == 7)).first() is not None


def test_immediate_connection_rolls_back_on_error(migrated: Settings) -> None:
    with pytest.raises(RuntimeError), immediate_connection(migrated) as connection:
        connection.execute(
            Title.__table__.insert().values(kind="movie", arr_id=8, title="Film", enabled=False)
        )
        raise RuntimeError("boom")
    with session_scope() as session:
        assert session.scalars(select(Title).where(Title.arr_id == 8)).first() is None


def test_a_second_immediate_transaction_waits_then_fails(migrated: Settings) -> None:
    """The write lock is exclusive, and `busy_timeout` -- not an instant error -- is
    what a contending claimer hits. `claim_next` turns this into "no work"."""
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with immediate_connection(migrated):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        with (
            pytest.raises(OperationalError, match="locked"),
            immediate_connection(migrated, busy_timeout_ms=50),
        ):
            pass
    finally:
        release.set()
        holder.join(5)


# ------------------------------------------------------------------- the clock


def test_utcnow_is_naive(migrated: Settings) -> None:
    """`DateTime(timezone=True)` is a no-op on SQLite, so an aware value read back
    would raise TypeError the moment it met `datetime.now(UTC)`."""
    assert utcnow().tzinfo is None


def test_staleness_arithmetic_works_for_both_writers(migrated: Settings) -> None:
    """`func.now()` writes 19 characters and Python writes 26. Both must compare."""
    with session_scope() as session:
        item_id = _item(session).id
        session.add(
            Job(
                id="server",
                media_item_id=item_id,
                trigger="manual",
                state="done",
                heartbeat=func.now(),
            )
        )
        session.flush()
        session.add(
            Job(
                id="python",
                media_item_id=item_id,
                trigger="manual",
                state="failed",
                heartbeat=utcnow() - timedelta(seconds=300),
            )
        )
        session.flush()

    with session_scope() as session:
        cutoff = utcnow() - timedelta(seconds=120)
        stale = set(session.scalars(select(Job.id).where(Job.heartbeat < cutoff)))
        assert stale == {"python"}
        # And in Python, which is where the TypeError would have surfaced.
        for job in session.scalars(select(Job)).all():
            assert isinstance(utcnow() - job.heartbeat, timedelta)
