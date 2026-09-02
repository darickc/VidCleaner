"""A complete, fake stage registry.

`StageContext.stage_registry` is per-context and overridable, which is what makes
this possible: the whole worker run loop -- claim, state transitions, progress,
retry classification, resume, persistence, work-dir pruning -- can be driven end to
end with **no ffmpeg and no torch**, and in milliseconds.

Each module writes the artifact its real counterpart would, using the real artifact
models, so the runner's readers are exercised for real. `CONTROL` lets a test make a
chosen stage fail, or make `probe` report `already_clean`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STAGES = (
    "probe",
    "extract",
    "subtitles",
    "transcribe",
    "detect",
    "render",
    "verify",
    "swap",
    "refresh",
)

REGISTRY = {stage: f"tests.support.stages.{stage}" for stage in STAGES}


@dataclass
class Control:
    fail: dict[str, BaseException] = field(default_factory=dict)
    already_clean: bool = False
    ran: list[str] = field(default_factory=list)
    source_size: int = 4096
    out_size: int = 5000
    verify_ok: bool = True
    detections: int = 3
    extras: dict[str, Any] = field(default_factory=dict)

    def reset(self) -> None:
        self.fail.clear()
        self.already_clean = False
        self.ran.clear()
        self.verify_ok = True
        self.detections = 3
        self.extras.clear()

    def enter(self, stage: str) -> None:
        self.ran.append(stage)
        exc = self.fail.get(stage)
        if exc is not None:
            raise exc


CONTROL = Control()
