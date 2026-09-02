"""What to do when a stage fails -- §6's retry rules, made concrete.

§6 says only "Transient API failures retry with backoff; render/verify failures are
terminal until reprocess". Turning that into behaviour needs three things it does not
say: which failures count as transient, how long the backoff is, and what happens to a
job whose *last* stage failed after the library was already updated.

The last one is the interesting case. Once ``swap`` has committed, the library file is
correct; a failure in ``refresh`` or ``snippets`` after that must not mark the job
failed and must not re-run the swap. §6 step 9 already says "warn" for the refresh
mapping check -- this generalises it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from vidcleaner.pipeline.stages import StaleSourceError, SwapBrokenError

#: Re-exported so a caller reasoning about retries needs one import, not two.
from vidcleaner.worker.claim import MAX_ATTEMPTS

__all__ = [
    "BACKOFF_S",
    "BEST_EFFORT_STAGES",
    "MAX_ATTEMPTS",
    "TERMINAL_STAGES",
    "Outcome",
    "backoff_for",
    "classify",
]

#: The swap already committed, so the library is right and the job is done. Failing
#: here costs a stale Jellyfin entry until the hourly sync, not a wrong file.
BEST_EFFORT_STAGES: Final = frozenset({"refresh", "snippets"})
#: Retrying these re-does 20 minutes of encoding to fail identically (render/verify),
#: or re-runs renames over a half-changed library (swap). Recovery is an explicit
#: reprocess, which is what §9.3's button is for.
TERMINAL_STAGES: Final = frozenset({"render", "verify", "swap"})
#: Per attempt already made. Long enough that a restarting Sonarr is back.
BACKOFF_S: Final = (60.0, 300.0, 900.0)

Verdict = Literal["retry", "terminal", "stale", "best_effort"]


@dataclass(frozen=True, slots=True)
class Outcome:
    verdict: Verdict
    state: str
    """The ``jobs.state`` to write. ``queued`` for a retry."""
    retry_in_s: float | None = None
    detail: str = ""


def backoff_for(attempts: int) -> float:
    index = max(0, min(attempts - 1, len(BACKOFF_S) - 1))
    return BACKOFF_S[index]


def classify(stage: str, exc: BaseException, *, attempts: int) -> Outcome:
    """Decide a failed stage's fate.

    ``attempts`` is the claim count (see ``Job``'s docstring), so the poison-job guard
    counts crashes as well as failures.
    """
    if isinstance(exc, SwapBrokenError):
        # Both a library rename and its rollback failed. A retry cannot help and could
        # do more damage; a human has to look at the paths in the message.
        return Outcome("terminal", "failed", detail="swap broken; manual recovery required")

    if isinstance(exc, StaleSourceError):
        # §6's "path vanished": re-resolve and requeue once, then give up. One retry,
        # because the arr needs a moment to finish whatever moved the file.
        if attempts <= 1:
            return Outcome("stale", "queued", retry_in_s=60.0, detail="source changed; requeueing")
        return Outcome("stale", "stale", detail="source is gone or keeps changing")

    if stage in BEST_EFFORT_STAGES:
        return Outcome("best_effort", "done", detail=f"{stage} failed after the swap committed")

    if stage in TERMINAL_STAGES:
        return Outcome("terminal", "failed", detail=f"{stage} failed; reprocess to retry")

    if attempts >= MAX_ATTEMPTS:
        return Outcome("terminal", "failed", detail=f"gave up after {attempts} attempts")

    return Outcome("retry", "queued", retry_in_s=backoff_for(attempts), detail=f"retrying {stage}")
