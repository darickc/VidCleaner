"""The per-job scratch directory ``/work/<job_id>/`` and its stage markers.

This lives in ``pipeline/`` rather than ``config.py`` because ``config.py`` is
deployment configuration -- imported by everything and deliberately ignorant of
job layout -- and rather than ``stages.py`` because M4's API will want to read
``detections.json`` and the snippet files without importing the stage registry
(and through it ffmpeg and torch). ``workspace`` depends only on ``config``.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vidcleaner import __version__
from vidcleaner.config import Settings, get_settings
from vidcleaner.db.constants import JOB_STAGES

__all__ = ["MARKER_SUFFIX", "StageMarker", "Workspace", "atomic_write_bytes", "atomic_write_text"]

MARKER_SUFFIX = ".done"


class StageMarker(BaseModel):
    """Written as ``<stage>.done`` when a stage completes."""

    model_config = ConfigDict(extra="ignore")

    stage: str
    version: str
    finished_at: datetime
    elapsed_s: float = 0.0
    detail: dict = Field(default_factory=dict)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then ``os.replace``.

    Resume correctness depends on this: a half-written artifact next to a
    completed marker would be read back as if it were whole.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


class Workspace:
    """Names every artifact in one job's scratch directory."""

    __slots__ = ("job_id", "root")

    def __init__(self, job_id: str, root: Path) -> None:
        self.job_id = job_id
        self.root = root

    @classmethod
    def for_job(cls, job_id: str, settings: Settings | None = None) -> Workspace:
        base = (settings or get_settings()).work_dir
        return cls(job_id, base / job_id)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Workspace(job_id={self.job_id!r}, root={str(self.root)!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Workspace) and other.job_id == self.job_id and other.root == self.root
        )

    def __hash__(self) -> int:
        return hash((self.job_id, self.root))

    def ensure(self) -> Workspace:
        for path in (self.root, self.subs_dir, self.redacted_dir, self.snippets_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self

    # ------------------------------------------------------------- artifacts

    def path(self, name: str) -> Path:
        return self.root / name

    @property
    def job_spec(self) -> Path:
        """Artifact zero: makes the work dir self-describing, so resume never needs the DB."""
        return self.root / "job.json"

    @property
    def probe_json(self) -> Path:
        return self.root / "probe.json"

    @property
    def audio_wav(self) -> Path:
        return self.root / "audio.wav"

    @property
    def subs_json(self) -> Path:
        return self.root / "subs.json"

    @property
    def transcript_json(self) -> Path:
        return self.root / "transcript.json"

    @property
    def detections_json(self) -> Path:
        return self.root / "detections.json"

    @property
    def graph_txt(self) -> Path:
        return self.root / "graph.txt"

    @property
    def render_json(self) -> Path:
        return self.root / "render.json"

    @property
    def verify_json(self) -> Path:
        return self.root / "verify.json"

    @property
    def out_mkv(self) -> Path:
        return self.root / "out.mkv"

    @property
    def ffmpeg_log(self) -> Path:
        return self.root / "ffmpeg.log"

    @property
    def subs_dir(self) -> Path:
        """Subtitle streams extracted from the source, in their source format."""
        return self.root / "subs"

    @property
    def redacted_dir(self) -> Path:
        """Redacted subtitles. Never the library -- only `swap.py` writes there."""
        return self.root / "redacted"

    @property
    def snippets_dir(self) -> Path:
        return self.root / "snippets"

    # --------------------------------------------------------------- markers

    def marker(self, stage: str) -> Path:
        if stage not in JOB_STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {JOB_STAGES}")
        return self.root / f"{stage}{MARKER_SUFFIX}"

    def read_marker(self, stage: str) -> StageMarker | None:
        path = self.marker(stage)
        if not path.is_file():
            return None
        try:
            return StageMarker.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_done(self, stage: str) -> bool:
        """True only when the marker exists *and* was written by this code version.

        The version check matters: resuming onto artifacts produced by different
        code silently mixes formats, and the failure surfaces far from the cause.
        """
        marker = self.read_marker(stage)
        return marker is not None and marker.version == __version__

    def mark_done(self, stage: str, *, elapsed_s: float = 0.0, detail: dict | None = None) -> None:
        marker = StageMarker(
            stage=stage,
            version=__version__,
            finished_at=datetime.now(UTC),
            elapsed_s=elapsed_s,
            detail=detail or {},
        )
        atomic_write_text(self.marker(stage), marker.model_dump_json(indent=2))

    def completed_stages(self) -> tuple[str, ...]:
        return tuple(s for s in JOB_STAGES if self.is_done(s))

    def clear_from(self, stage: str) -> None:
        """Drop the markers for ``stage`` and every later stage.

        Ordering comes from ``db.constants.JOB_STAGES``, which is the authority;
        it is imported, never redefined.
        """
        if stage not in JOB_STAGES:
            raise ValueError(f"unknown stage {stage!r}; expected one of {JOB_STAGES}")
        for later in JOB_STAGES[JOB_STAGES.index(stage) :]:
            self.marker(later).unlink(missing_ok=True)

    def clear_all(self) -> None:
        for stage in JOB_STAGES:
            self.marker(stage).unlink(missing_ok=True)

    # ----------------------------------------------------------------- disk

    def free_bytes(self) -> int:
        probe = self.root if self.root.is_dir() else self.root.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return 0

    def size_bytes(self) -> int:
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total
