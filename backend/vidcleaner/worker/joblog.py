"""The `job_logs` timeline -- what §9.1's log tail and §9.4's job log show.

Two log sinks exist and they carry different things. Raw ffmpeg stderr and argv go to
``/work/<job_id>/ffmpeg.log`` via ``FFmpegRunner(log_path=...)``, because it is
verbose, uninteresting until something breaks, and would bloat the database. The
``job_logs`` table gets the few dozen lines a human would actually want: stage
boundaries with timings, each stage's own one-line summary, warnings, and every state
transition with its reason.

Rows are **buffered and flushed at stage boundaries**. One transaction per log line
would take SQLite's write lock dozens of times during a job, and the claim in the
other process fails with "database is locked" if anyone holds it too long.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vidcleaner.config import Settings
from vidcleaner.db.models import JobLog
from vidcleaner.db.session import session_scope, utcnow
from vidcleaner.logging import get_logger

__all__ = ["Timeline"]

log = get_logger(__name__)


@dataclass
class _Row:
    level: str
    msg: str
    ts: Any


@dataclass
class Timeline:
    """Buffered writer for one job's timeline. Every call also logs to structlog,
    so one line feeds both the container log and the UI."""

    job_id: str
    settings: Settings | None = None
    _rows: list[_Row] = field(default_factory=list, repr=False)

    def info(self, msg: str, **kw: Any) -> None:
        self._add("info", msg, kw)

    def warning(self, msg: str, **kw: Any) -> None:
        self._add("warning", msg, kw)

    def error(self, msg: str, **kw: Any) -> None:
        self._add("error", msg, kw)

    def _add(self, level: str, msg: str, kw: dict[str, Any]) -> None:
        detail = " ".join(f"{k}={v}" for k, v in kw.items() if v is not None)
        line = f"{msg}  {detail}".strip() if detail else msg
        self._rows.append(_Row(level, line[:2000], utcnow()))
        getattr(log, level)("job.event", job_id=self.job_id, msg=msg, **kw)

    @property
    def pending(self) -> int:
        return len(self._rows)

    def flush(self) -> int:
        """Write the buffer. Never raises: bookkeeping must not fail a good render."""
        if not self._rows:
            return 0
        rows, self._rows = self._rows, []
        try:
            with session_scope() as session:
                for row in rows:
                    session.add(JobLog(job_id=self.job_id, level=row.level, msg=row.msg, ts=row.ts))
        except Exception:  # noqa: BLE001 - the job's outcome does not depend on this
            log.warning("joblog.flush_failed", job_id=self.job_id, dropped=len(rows))
            return 0
        return len(rows)

    def __enter__(self) -> Timeline:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.flush()
