"""`refresh`, for real. Skips itself when no integrations are injected."""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.refresh import NAME, load
from vidcleaner.pipeline.refresh import run as _run

__all__ = ["NAME", "load", "run"]


def run(ctx) -> None:
    CONTROL.enter(NAME)
    _run(ctx)
