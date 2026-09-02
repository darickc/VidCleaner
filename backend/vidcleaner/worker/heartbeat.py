"""The per-job monitor thread: heartbeat, observable progress, and cancellation.

§4 says "heartbeat every 30 s". Doing that from the ``on_progress`` callback does not
work, for three reasons that are all in the existing code rather than hypothetical:

* ``verify`` emits no progress at all and runs a full decode of the output -- minutes
  on a feature film.
* §6.1's ``wait_for_stable`` blocks for up to 300 s inside ``time.sleep``.
* ``probe``, ``subtitles`` and ``detect`` emit nothing either.

So a thread. Making it the **only** writer of the job row while the job runs pays for
itself twice: ``report()`` becomes a lock-free store rather than a database write, so
ffmpeg's roughly-per-second callbacks need no throttling logic; and the same tick that
writes the heartbeat can read back the state, which is the only cross-process channel
available for "someone cancelled this job".

Threading is safe here because SQLAlchemy passes ``check_same_thread=False`` and uses
``QueuePool`` for *file* SQLite URLs. Tests must therefore keep using the file-backed
fixture; an in-memory URL flips to ``SingletonThreadPool`` and breaks this.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import select

from vidcleaner.config import Settings
from vidcleaner.db.constants import TERMINAL_STATES
from vidcleaner.db.models import Job
from vidcleaner.db.session import get_engine
from vidcleaner.logging import get_logger
from vidcleaner.worker.claim import HEARTBEAT_TICK_S, heartbeat
from vidcleaner.worker.progress import ProgressTracker

__all__ = ["JobMonitor"]

log = get_logger(__name__)


class JobMonitor:
    """One thread per running job. Also the job's control channel.

    Use as a context manager. ``report`` is the ``on_progress`` callback to hand to
    ``build_context``; ``cancelled`` is what the runner checks between stages.
    """

    def __init__(
        self,
        *,
        job_id: str,
        worker_id: str,
        tracker: ProgressTracker,
        settings: Settings | None = None,
        tick_s: float = HEARTBEAT_TICK_S,
    ) -> None:
        self.job_id = job_id
        self.worker_id = worker_id
        self.tracker = tracker
        self.settings = settings
        self.tick_s = tick_s
        self._lock = threading.Lock()
        self._stage: str | None = None
        self._state: str | None = None
        self._pct = tracker.value
        self._cancelled = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reason: str | None = None

    # ------------------------------------------------------------- reporting

    def set_stage(self, stage: str, state: str) -> None:
        """Publish a stage transition immediately, rather than on the next tick.

        There are at most ten of these per job -- nothing beside ffmpeg's
        roughly-per-second progress callbacks -- and they are the moments §9.1's
        Queue page actually needs. Without it a job that finishes in under one tick
        never records a stage at all, so the row still says `probing` when it is
        done, and a crashed job's `state` is no guide to where it stopped.
        """
        with self._lock:
            self._stage = stage
            self._state = state
            self._pct = self.tracker.absolute(stage, 0.0)
        self.beat()

    def report(self, stage: str, fraction: float) -> None:
        """``on_progress``. Deliberately does no I/O: the thread owns the writes."""
        with self._lock:
            self._pct = self.tracker.absolute(stage, fraction)
            self._stage = stage

    def snapshot(self) -> tuple[str | None, str | None, float]:
        with self._lock:
            return self._stage, self._state, self._pct

    # ---------------------------------------------------------- cancellation

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def reason(self) -> str | None:
        """Why we should stop: ``cancelled`` (someone closed the job) or ``fenced``."""
        return self._reason

    def _flag(self, reason: str) -> None:
        if not self._cancelled.is_set():
            self._reason = reason
            self._cancelled.set()
            log.warning("job.abort_requested", job_id=self.job_id, reason=reason)

    # ------------------------------------------------------------------ tick

    def beat(self) -> bool:
        """One heartbeat plus one state read. Returns False when we must stop.

        Public so tests can drive it without threads or sleeping.
        """
        stage, state, pct = self.snapshot()
        owned = heartbeat(
            job_id=self.job_id,
            worker_id=self.worker_id,
            state=state,
            stage=stage,
            progress_pct=pct,
            settings=self.settings,
        )
        if not owned:
            self._flag("fenced")
            return False
        if self._observed_state() in TERMINAL_STATES:
            self._flag("cancelled")
            return False
        return True

    def _observed_state(self) -> str | None:
        try:
            with get_engine(self.settings).connect() as connection:
                return connection.execute(
                    select(Job.state).where(Job.id == self.job_id)
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 - a monitor must never fail a job
            log.warning("job.state_read_failed", job_id=self.job_id, exc_info=True)
            return None

    # -------------------------------------------------------------- lifecycle

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.beat():
                return
            self._stop.wait(self.tick_s)

    def start(self) -> JobMonitor:
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.job_id[:8]}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.tick_s + 5)
            self._thread = None

    def __enter__(self) -> JobMonitor:
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()
