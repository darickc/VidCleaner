"""§6's audit pass: comparing a full-file re-check against what a file already has.

**The finding that shapes this module.** ``detect(mode="audit")`` takes the ``_full``
branch, which is ``_stt_only(existing=())`` -- subtitle hits are ignored entirely. §11's
M2 demo measured exactly that comparison on the test episode:

    detections: windowed 49, full 40. "Full mode missed 22 windowed detections and found
    13 the windowed run missed. That is the strongest evidence yet for §6's audit pass:
    the union beats either mode alone."

So an audit that simply re-rendered from its own detection set would replace a 49-word
mute with a 40-word one and make the file **worse**, with 22 previously-muted words
audible again. §6's word is "*adds* any detections the subtitles missed", and that means
a union. Hence this module: the audit's output is the **merge** of the evidence the file
already carries and what the full pass heard, never a replacement.

Pure -- no database, no ffmpeg, no torch -- for the same reason ``detect.py`` is: the
whole comparison is unit-testable against hand-built detections in microseconds, which
is the only way to test an audit without a 30-minute speech-to-text run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from vidcleaner.pipeline.artifacts import Detection, DetectionResult
from vidcleaner.pipeline.detect import DetectOptions, finalize, related_canonical

__all__ = [
    "AUDIT_TIME_SLACK_S",
    "AuditComparison",
    "AuditOptions",
    "compare",
    "covers",
    "merged_result",
]

#: How far a found detection may sit from a prior mute and still count as covered.
#: M2 measured cross-mode timing drift at median +0.009 s and mean +0.083 s, but a
#: **max of 0.74 s** -- so a slack under that would report the same word twice
#: whenever the two passes disagreed at the tail of the distribution.
AUDIT_TIME_SLACK_S: Final = 1.0


@dataclass(frozen=True, slots=True)
class AuditOptions:
    time_slack_s: float = AUDIT_TIME_SLACK_S
    min_confidence: float = 0.0
    """Gates **promotion only** (see :attr:`AuditComparison.promotable`), never what is
    recorded. From M2's unresolved note that three of its 13 full-only detections
    scored below 0.01, "which looks like recognition noise". Default 0.0 because that
    threshold is unmeasured, and a default that silently drops findings would be worse
    than one that occasionally re-renders for nothing."""


@dataclass(frozen=True, slots=True)
class AuditComparison:
    new: tuple[Detection, ...] = ()
    """Heard by the full pass, not covered by anything the file already has."""
    improved: tuple[tuple[Detection, Detection], ...] = ()
    """``(prior, better)`` -- the same word, located more precisely this time."""
    carried: tuple[Detection, ...] = ()
    """Prior detections kept as they are, already guarded and padded."""
    _floor: float = field(default=0.0, repr=False)
    """``AuditOptions.min_confidence``, carried so :attr:`promotable` is self-contained."""

    @property
    def promotable(self) -> tuple[Detection, ...]:
        """The new detections that would actually change the audio.

        A hit that is not muted (a censored token with ``mute_censored_tokens`` off, or
        a whitelisted word) changes nothing, so re-rendering for it is pure waste. The
        confidence floor applies here and only here.
        """
        return tuple(
            d
            for d in self.new
            if d.muted
            and not d.whitelisted
            and (d.confidence is None or d.confidence >= self._floor)
        )

    @property
    def should_render(self) -> bool:
        """Whether §6's "if new hits appear, it re-renders" is satisfied.

        An *improvement* alone deliberately does not qualify. M2 measured
        subtitle-only fallbacks at +0.551 s error, smearing 1.2-1.9 s across a ~0.3 s
        word, so tightening one is a real gain -- but not worth a swap, an arr rescan
        and a Jellyfin refresh on its own.
        """
        return bool(self.promotable)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "promotable": len(self.promotable),
            "improved": len(self.improved),
            "carried": len(self.carried),
        }


def covers(prior: Detection, found: Detection, *, slack_s: float = AUDIT_TIME_SLACK_S) -> bool:
    """Does ``prior`` already account for ``found``?

    Compares ``found``'s **unpadded** span against ``prior``'s **padded** one, widened
    by ``slack_s``. Padded is the right side to use: if the previous run already mutes
    that stretch of audio then there is nothing new for the user to hear, whatever the
    two passes think the word's exact boundaries were.

    Word identity is ``detect.related_canonical``, the same predicate the detector uses
    to deduplicate its own STT-only hits -- so a bare ``god`` inside a partially located
    ``god damn`` is treated as the same finding rather than a new one. Note it is the
    *identity* half only: ``detect.same_finding`` bundles an **unpadded** overlap check,
    which would veto precisely the cases the padding above is here to catch.
    """
    start = prior.mute_start_s - slack_s
    end = prior.mute_end_s + slack_s
    if found.end_s < start or end < found.start_s:
        return False
    return related_canonical(prior.word_canonical, found.word_canonical)


def _is_improvement(prior: Detection, found: Detection) -> bool:
    """A precisely located hit replacing a wide subtitle guess.

    Only ever narrows, and only where the prior span came from the subtitle fallback
    that M2 showed to be the inaccurate one. An STT-located prior is left alone: two
    STT passes disagreeing by 100 ms is noise, not an improvement.
    """
    if prior.source != "subtitle" or found.source == "subtitle":
        return False
    return (found.end_s - found.start_s) < (prior.end_s - prior.start_s)


def compare(
    prior: Sequence[Detection],
    found: Sequence[Detection],
    *,
    opts: AuditOptions | None = None,
) -> AuditComparison:
    """Split ``found`` into what is new and what the file already covers.

    ``prior`` is the evidence set the file carries today (the DB rows of the job
    ``views.evidence_job_ids`` picks), ``found`` is what the full-file pass heard.
    """
    opts = opts or AuditOptions()
    new: list[Detection] = []
    improved: list[tuple[Detection, Detection]] = []
    claimed: set[int] = set()

    for candidate in found:
        match: Detection | None = None
        for index, existing in enumerate(prior):
            if index in claimed:
                continue
            if covers(existing, candidate, slack_s=opts.time_slack_s):
                match = existing
                claimed.add(index)
                break
        if match is None:
            new.append(candidate)
        elif _is_improvement(match, candidate):
            improved.append((match, candidate))

    tightened = {id(p) for p, _ in improved}
    carried = tuple(p for p in prior if id(p) not in tightened)
    return AuditComparison(
        new=tuple(new),
        improved=tuple(improved),
        carried=carried,
        _floor=opts.min_confidence,
    )


def merged_result(
    comparison: AuditComparison,
    *,
    profile_hash: str = "",
    duration_s: float = 0.0,
    detect_opts: DetectOptions | None = None,
) -> DetectionResult:
    """The union, finished exactly the way ``detect`` finishes its own output.

    **Everything here is already finished.** Both sides of the comparison came out of a
    completed ``detect`` stage, which ends in ``finalize`` -- so both were guarded and
    padded once, and ``_pad`` leaves ``start_s``/``end_s`` alone while setting
    ``mute_start_s``/``mute_end_s``. Passing any of it back through the guard-and-pad
    path would widen every mute by another ``pad_pre + pad_post`` on each audit, which
    over a few passes would swallow the surrounding dialogue.

    What this call *is* for is the one thing that genuinely has to be redone over the
    union: ``merge_ranges``. Two adjacent detections found by different passes must
    merge into one range, and the counts, stats and ``total_muted_s`` have to describe
    the merged set. Doing it through ``finalize`` is what keeps ``render``'s ranges and
    ``verify``'s windows coming from exactly one place.
    """
    preserved = [
        *comparison.carried,
        *(better for _, better in comparison.improved),
        *comparison.new,
    ]
    return finalize(
        [],
        preserved=preserved,
        duration_s=duration_s,
        opts=detect_opts or DetectOptions(),
        profile_hash=profile_hash,
    )
