"""The M0 worker: it must connect, idle, and stop promptly when asked."""

from __future__ import annotations

import threading

from vidcleaner.config import Settings
from vidcleaner.worker.runner import Worker, worker_id


def test_worker_id_is_host_and_pid() -> None:
    assert ":" in worker_id()


def test_run_exits_when_stop_is_requested(migrated: Settings) -> None:
    worker = Worker(migrated, poll_interval=0.05)
    thread = threading.Thread(target=worker.run)
    thread.start()
    worker.request_stop()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_poll_once_finds_nothing_on_an_empty_queue(migrated: Settings) -> None:
    """The queue behaviour itself lives in tests/unit/test_worker_run.py."""
    assert Worker(migrated).poll_once() is False


def test_the_worker_registers_the_swap_reconciler(migrated: Settings) -> None:
    """Stale recovery cannot resolve an interrupted swap without it, and the queue
    deliberately does not import the pipeline to get it (the api imports the queue)."""
    from vidcleaner.worker import claim

    claim.SWAP_RECONCILER = None
    try:
        Worker(migrated)
        assert claim.SWAP_RECONCILER is not None
    finally:
        claim.SWAP_RECONCILER = None


def test_check_database_succeeds_after_migration(migrated: Settings) -> None:
    Worker(migrated).check_database()
