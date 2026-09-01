"""Worker main loop.

M0 stands the process up and proves it can reach the database; claiming and the
pipeline stages arrive with their milestones. It is a separate process from the api
because CTranslate2/numpy hold the GIL and pin threads, which would starve the event
loop (PLAN.md §4).
"""

from __future__ import annotations

import os
import socket
import threading

from sqlalchemy import text

from vidcleaner.config import Settings, get_settings
from vidcleaner.db.session import get_engine
from vidcleaner.logging import get_logger

log = get_logger(__name__)

POLL_INTERVAL_S = 5.0


def worker_id() -> str:
    """Identifies this process in ``jobs.claimed_by``."""
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker:
    def __init__(self, settings: Settings | None = None, poll_interval: float = POLL_INTERVAL_S):
        self.settings = settings or get_settings()
        self.poll_interval = poll_interval
        self.id = worker_id()
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Signal-safe: wakes the loop out of its sleep so shutdown is immediate."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def check_database(self) -> None:
        with get_engine(self.settings).connect() as connection:
            connection.execute(text("SELECT 1"))

    def poll_once(self) -> bool:
        """Claim and run one job. Returns True if work was done.

        TODO(M3): implement the claim from PLAN.md §4 --
        ``BEGIN IMMEDIATE; UPDATE jobs SET state=..., claimed_by=?, heartbeat=now
        WHERE id=(SELECT id ... WHERE state='queued' ORDER BY priority, created_at
        LIMIT 1)``, then run the stage machine from §6 with heartbeats every 30 s.
        """
        return False

    def run(self) -> None:
        self.check_database()
        log.info(
            "worker.startup",
            worker_id=self.id,
            work_dir=str(self.settings.work_dir),
            poll_interval_s=self.poll_interval,
        )
        while not self.stopping:
            try:
                did_work = self.poll_once()
            except Exception:
                log.exception("worker.poll_failed")
                did_work = False
            if not did_work:
                self._stop.wait(self.poll_interval)
        log.info("worker.shutdown", worker_id=self.id)
