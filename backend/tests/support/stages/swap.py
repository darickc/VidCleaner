"""`swap`, for real.

Not faked: it is pure filesystem work behind the injectable `FsOps`, and the point of
a run-loop test that reaches it is to see the real transaction run.
"""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.swap import NAME, load
from vidcleaner.pipeline.swap import run as _run

__all__ = ["NAME", "load", "run"]


def run(ctx) -> None:
    CONTROL.enter(NAME)
    _run(ctx)
