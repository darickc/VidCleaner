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


def test_poll_once_does_no_work_yet(migrated: Settings) -> None:
    # Guards the M0 contract: no queue behaviour until M3 implements claiming.
    assert Worker(migrated).poll_once() is False


def test_check_database_succeeds_after_migration(migrated: Settings) -> None:
    Worker(migrated).check_database()
