"""Turning per-stage progress into one number for `jobs.progress_pct`.

PLAN.md never defines ``progress_pct``. The stages emit ``on_progress(stage,
fraction)`` (§4's seam) but they take wildly different amounts of time, so averaging
them would make a 56-minute transcription and a 0.1-second probe worth the same.

Weights come from the M1 and M2 demo timings in the Decision Log. Two tables, because
a full-file pass is 20-40x a windowed one: with the windowed weights, a full job would
sit at 6% for two hours.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

__all__ = ["STAGE_WEIGHTS_FULL", "STAGE_WEIGHTS_WINDOWED", "ProgressTracker"]

#: Relative cost per stage. From the M1 demo: probe 0.1 s, extract 3.9 s, subtitles
#: 0.4 s, transcribe 67.4 s, detect 0.0 s, render 18.5 s, verify 4.4 s.
STAGE_WEIGHTS_WINDOWED: Final[Mapping[str, float]] = {
    "probe": 1,
    "extract": 5,
    "subtitles": 3,
    "transcribe": 60,
    "detect": 1,
    "render": 20,
    "verify": 8,
    "swap": 2,
    "refresh": 1,
    "snippets": 3,
}
#: M2's demo: 1173 s of full-file STT against 67 s windowed, on the same episode.
STAGE_WEIGHTS_FULL: Final[Mapping[str, float]] = {**STAGE_WEIGHTS_WINDOWED, "transcribe": 400}


class ProgressTracker:
    """Absolute percentage across a planned stage list.

    Two properties that are easy to get wrong and both visible in the UI:

    * **Monotonic.** A stage that reports 0.0 after an earlier stage finished must not
      drag the bar backwards.
    * **A resumed job starts from its markers, not from zero.** ``run_stage`` returns
      early when a marker is present and deliberately does *not* call ``progress``, so
      without a floor a job resuming at ``render`` would report 0% until render ended.
    """

    __slots__ = ("_max", "_offsets", "_planned", "_total", "_weights")

    def __init__(
        self,
        planned: Sequence[str],
        *,
        weights: Mapping[str, float] | None = None,
        completed: Iterable[str] = (),
    ) -> None:
        self._planned = list(planned)
        self._weights = dict(weights or STAGE_WEIGHTS_WINDOWED)
        self._recompute()
        self._max = 0.0
        self.complete_all(completed)

    def _recompute(self) -> None:
        self._total = sum(self._weight(s) for s in self._planned) or 1.0
        running = 0.0
        self._offsets = {}
        for stage in self._planned:
            self._offsets[stage] = running
            running += self._weight(stage)

    def _weight(self, stage: str) -> float:
        return float(self._weights.get(stage, 1))

    def reweight(self, weights: Mapping[str, float]) -> None:
        """Switch tables mid-run, for a job auto-promoted to a full pass.

        The promotion is decided inside the ``transcribe`` stage from the subtitle
        result, so the runner cannot know at claim time which table applies.
        """
        self._weights = dict(weights)
        self._recompute()

    def absolute(self, stage: str, fraction: float) -> float:
        """Percentage complete, given ``stage`` is ``fraction`` done."""
        if stage not in self._offsets:
            return self._max
        fraction = max(0.0, min(1.0, fraction))
        value = 100.0 * (self._offsets[stage] + self._weight(stage) * fraction) / self._total
        self._max = max(self._max, value)
        return self._max

    def complete(self, stage: str) -> float:
        return self.absolute(stage, 1.0)

    def complete_all(self, stages: Iterable[str]) -> float:
        for stage in stages:
            self.complete(stage)
        return self._max

    @property
    def value(self) -> float:
        return self._max
