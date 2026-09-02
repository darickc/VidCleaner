"""The subtitle drift check (PLAN.md §6 step 3).

Subtitle cues can be offset, or drift across a file when the subs were authored
for a different frame rate or a different cut. M1 assumed they were correctly
timed, so a drifted track cut its STT windows around silence and every word fell
back to the 0.3-confidence proportional span.

This module measures the offset and decides how much to trust the cues. It is
almost entirely pure -- ``measure_drift`` is the only function that touches a
transcriber -- so the arithmetic is unit-testable with no torch, no ffmpeg and
no speech, which matters because every generated fixture is a sine tone.

Three corrections to §6, each recorded in the Decision Log
=========================================================

* §6's **"text similarity < 0.4"** cannot work as written. A cue is transcribed
  with a ±5 s pad, so the window text is several times longer than the cue and a
  whole-string ratio is dominated by the pad rather than the match. Measured:
  ``fuzz.ratio`` scores a *perfect* match at **28.7** against 22.6 for unrelated
  dialogue -- both below §6's own threshold, so the rule as written would discard
  every subtitle track in the library. Replaced by **alignment coverage**: the
  fraction of the cue's words that were actually found in the audio. It is
  pad-invariant, lies in [0, 1], and falls out of the alignment for free.
* §6's **"spread > 0.5 s"** does not say spread of what. Defined here as the
  spread of the **per-probe median offsets** -- which is the whole reason for
  sampling at 10/50/90%: a constant offset is correctable, a *growing* one means
  the subtitles are for another cut and no single number will fix them.
* §6 lists window building before the drift check, but the drift verdict chooses
  the window padding and the offset. The real order is cues → hits → drift →
  windows.

Pairing subtitle words to spoken words
======================================

Subtitles carry no per-word times, so the subtitle side is *estimated* by
interpolating character offsets across the cue -- the same rule
``subtitles.find_hits`` uses, deliberately, so the number measured here is the
error the detector actually experiences.

The pairing itself is a **monotone sequence alignment** (``Levenshtein.opcodes``
over folded token lists), not nearest-time or greedy fuzzy matching. Both of
those break on the ordinary case of a cue that repeats a word -- "Bullshit.
Bull. Shit." is in the project's own test fixtures -- because neither is
order-preserving. The ``equal`` blocks of an edit script are exactly a monotone
one-to-one pairing, and the ``insert`` blocks absorb the extra speech the ±5 s
pad drags in.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

from vidcleaner.matching.normalize import fold, strip_wrappers
from vidcleaner.pipeline.artifacts import (
    DriftProbe,
    DriftResult,
    SubtitleCue,
    TimeRange,
    TranscriptWord,
)

NAME = "drift"

__all__ = [
    "MIN_ANCHOR_LEN",
    "PROBE_PAD_S",
    "ProbePlan",
    "SubWord",
    "Measurement",
    "align_probe",
    "cue_word_spans",
    "decide",
    "measure",
    "measure_drift",
    "plan_probes",
]

#: §6 step 3: three cues with at least six words, at 10/50/90% of the file.
PROBE_POSITIONS = (0.10, 0.50, 0.90)
MIN_PROBE_WORDS = 6
#: Relaxations, in order, when too few cues are that long. §6 offers none, and
#: short-form content would otherwise be punished for having terse subtitles.
FALLBACK_PROBE_WORDS = (4, 2)
#: Below this many usable probes the measurement is not reported at all.
MIN_PROBES = 2
PROBE_PAD_S = 5.0

#: Anchors shorter than this align by luck as often as by content ("a", "of",
#: "is") and, being the commonest words, would dominate the median.
MIN_ANCHOR_LEN = 3

#: §6 step 3's thresholds.
MAX_OFFSET_S = 0.7
MAX_SPREAD_S = 0.5
MIN_COVERAGE = 0.4


def _key(raw: str) -> str:
    """The comparison key for one word, at the same level on both sides.

    ``normalize``'s pipeline is ``strip_wrappers -> normalize -> fold``; calling
    ``fold`` alone leaves the outer punctuation on, so a cue's ``"Oh,"`` would
    never pair with a spoken ``"Oh"`` and every sentence-final word -- the ones
    whose timing matters most -- would silently drop out of the alignment.
    """
    return fold(strip_wrappers(raw))


@dataclass(frozen=True, slots=True)
class SubWord:
    """One subtitle word with its *estimated* time span."""

    fold: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class ProbePlan:
    cue_index: int
    span: TimeRange
    """The audio to transcribe: the cue padded by ``PROBE_PAD_S``."""
    words: tuple[SubWord, ...]
    text: str


@dataclass(frozen=True, slots=True)
class Observation:
    cue_index: int
    deltas: tuple[float, ...]
    coverage: float
    stt_text: str

    @property
    def median(self) -> float | None:
        return statistics.median(self.deltas) if self.deltas else None


# ----------------------------------------------------------------------- pure


def cue_word_spans(cue: SubtitleCue) -> list[SubWord]:
    """Split a cue into words with times interpolated from character offsets.

    The same proportional rule as ``subtitles.find_hits``, on purpose: the drift
    this measures has to be the drift the detector's fallback span suffers.
    """
    text = cue.text
    length = max(1, len(text))
    duration = cue.duration
    out: list[SubWord] = []
    cursor = 0
    for token in text.split():
        start_char = text.index(token, cursor)
        cursor = start_char + len(token)
        folded = _key(token)
        if not folded:
            continue
        out.append(
            SubWord(
                fold=folded,
                start=cue.start + duration * (start_char / length),
                end=cue.start + duration * (cursor / length),
            )
        )
    return out


def plan_probes(
    cues: Sequence[SubtitleCue],
    *,
    count: int = 3,
    positions: Sequence[float] = PROBE_POSITIONS,
    min_words: int = MIN_PROBE_WORDS,
    pad_s: float = PROBE_PAD_S,
    duration: float | None = None,
) -> list[ProbePlan]:
    """Pick the cues to probe: the longest-enough cue nearest each position."""
    if not cues:
        return []

    # Relax the word threshold until enough cues qualify, preferring longer cues:
    # more words mean more anchors mean a better median. Planning never refuses on
    # probe *count* -- whether two probes is enough to believe is ``decide``'s call.
    eligible: list[tuple[int, SubtitleCue]] = []
    for threshold in (min_words, *FALLBACK_PROBE_WORDS):
        candidates = [(i, c) for i, c in enumerate(cues) if len(c.text.split()) >= threshold]
        if len(candidates) > len(eligible):
            eligible = candidates
        if len(eligible) >= count:
            break
    if not eligible:
        return []

    chosen: dict[int, SubtitleCue] = {}
    for position in positions[:count]:
        target = position * (len(cues) - 1)
        index, cue = min(eligible, key=lambda pair: (abs(pair[0] - target), pair[0]))
        chosen.setdefault(cue.index, cue)
        if len(chosen) >= count:
            continue

    plans: list[ProbePlan] = []
    for cue in sorted(chosen.values(), key=lambda c: c.start):
        words = cue_word_spans(cue)
        if not words:
            continue
        end = cue.end + pad_s
        plans.append(
            ProbePlan(
                cue_index=cue.index,
                span=TimeRange(
                    start=max(0.0, cue.start - pad_s),
                    end=min(duration, end) if duration else end,
                ),
                words=tuple(words),
                text=cue.text,
            )
        )
    return plans


def align_probe(plan: ProbePlan, stt_words: Sequence[TranscriptWord]) -> Observation:
    """Pair cue words to spoken words by monotone alignment; return the deltas."""
    spoken = [(_key(w.word), w) for w in stt_words]
    spoken = [(f, w) for f, w in spoken if f]
    sub_folds = [w.fold for w in plan.words]
    stt_folds = [f for f, _ in spoken]

    deltas: list[float] = []
    paired = 0
    if sub_folds and stt_folds:
        for op in Levenshtein.opcodes(sub_folds, stt_folds):
            if op.tag != "equal":
                continue
            for step in range(op.src_end - op.src_start):
                sub_word = plan.words[op.src_start + step]
                _, stt_word = spoken[op.dest_start + step]
                paired += 1
                if len(sub_word.fold) >= MIN_ANCHOR_LEN:
                    deltas.append(stt_word.start - sub_word.start)

    coverage = paired / len(sub_folds) if sub_folds else 0.0
    return Observation(
        cue_index=plan.cue_index,
        deltas=tuple(deltas),
        coverage=coverage,
        stt_text=" ".join(f for f, _ in spoken),
    )


@dataclass(frozen=True, slots=True)
class Measurement:
    offset_s: float = 0.0
    spread_s: float = 0.0
    coverage: float = 0.0
    probes: int = 0
    """Probes actually transcribed. Distinct from ``timed``, and the distinction
    matters: a subtitle track for the wrong episode pairs *nothing*, so it has
    three probes and zero timings. Counting only timed probes reported that as
    "not checked" -- and therefore trustworthy -- which is exactly backwards for
    the case §6's similarity rule exists to catch."""
    timed: int = 0
    """Probes that yielded at least one anchor, i.e. that have an offset."""


def measure(observations: Iterable[Observation]) -> Measurement:
    """Aggregate probes into a :class:`Measurement`.

    ``spread`` is over the *per-probe* medians, not over every pair: a constant
    offset means the subs are merely shifted, whereas an offset that grows across
    the file means they belong to another cut and no single correction fixes them.
    """
    observations = list(observations)
    if not observations:
        return Measurement()

    medians = [o.median for o in observations if o.median is not None]
    all_deltas = [d for o in observations for d in o.deltas]
    return Measurement(
        offset_s=statistics.median(all_deltas) if all_deltas else 0.0,
        spread_s=(max(medians) - min(medians)) if len(medians) > 1 else 0.0,
        coverage=statistics.fmean(o.coverage for o in observations),
        probes=len(observations),
        timed=len(medians),
    )


def decide(
    measurement: Measurement,
    *,
    max_offset_s: float = MAX_OFFSET_S,
    max_spread_s: float = MAX_SPREAD_S,
    min_coverage: float = MIN_COVERAGE,
) -> tuple[str, str]:
    """The verdict. Returns ``(action, reason)``.

    Three outcomes, not §6's two. Step 3 says unreliable subtitles get widened
    windows; step 4 says unreliable subtitles go to a full pass. Those contradict,
    and the cause tells them apart: if the offset is *measurable* the cues are
    still worth using, just imprecisely, and widened windows cost a fraction of a
    full pass. Only cues that do not describe this audio at all are discarded.
    """
    if measurement.probes < MIN_PROBES:
        return "skipped", "too_few_probes"
    # Coverage first, and against *attempted* probes: a track that pairs nothing
    # is the strongest possible evidence that it does not belong to this audio.
    if measurement.coverage < min_coverage:
        return "discard", "coverage_below_threshold"
    if measurement.timed < MIN_PROBES:
        return "skipped", "too_few_anchors"
    if abs(measurement.offset_s) > max_offset_s:
        return "unreliable", "offset_above_threshold"
    if measurement.spread_s > max_spread_s:
        return "unreliable", "spread_above_threshold"
    return "ok", "within_thresholds"


def build_result(
    observations: Sequence[Observation],
    plans: Sequence[ProbePlan],
    *,
    model: str = "",
    elapsed_s: float = 0.0,
    **thresholds,
) -> DriftResult:
    """Assemble the artifact from the pure pieces."""
    measurement = measure(observations)
    action, reason = decide(measurement, **thresholds)
    by_index = {p.cue_index: p for p in plans}
    return DriftResult(
        checked=action != "skipped",
        model=model,
        action=action,  # type: ignore[arg-type]
        reason=reason,
        offset_s=round(measurement.offset_s, 4),
        spread_s=round(measurement.spread_s, 4),
        coverage=round(measurement.coverage, 4),
        elapsed_s=round(elapsed_s, 3),
        probes=[
            DriftProbe(
                cue_index=o.cue_index,
                span=by_index[o.cue_index].span if o.cue_index in by_index else None,
                cue_text=by_index[o.cue_index].text if o.cue_index in by_index else "",
                stt_text=o.stt_text[:500],
                pair_count=len(o.deltas),
                coverage=round(o.coverage, 4),
                median_offset_s=round(o.median, 4) if o.median is not None else None,
                deltas=[round(d, 4) for d in o.deltas[:20]],
            )
            for o in observations
        ],
    )


# ----------------------------------------------------------------- the shell


def measure_drift(ctx, cues: Sequence[SubtitleCue], probe) -> DriftResult:
    """The only impure function here: transcribe the probes and measure.

    All probes go into a **single** transcriber call -- ``clip_timestamps``
    already takes a list of windows -- so the cost is one model load and a few
    tens of seconds of audio, not one pass per probe.

    Any failure downgrades to "not checked" rather than failing the job. The
    measurement is an optimisation of the timing: losing it costs precision,
    which the detector's guards already absorb, while failing the job costs the
    whole episode.
    """
    import time  # noqa: PLC0415

    from vidcleaner.pipeline import lang  # noqa: PLC0415
    from vidcleaner.pipeline.stt import TranscribeRequest, model_for_mode  # noqa: PLC0415

    model = model_for_mode(ctx.settings, "drift")
    plans = plan_probes(cues, duration=probe.duration or None)
    if len(plans) < MIN_PROBES:
        return DriftResult(checked=False, model=model, action="skipped", reason="too_few_probes")
    if not ctx.ws.audio_wav.is_file():
        return DriftResult(checked=False, model=model, action="skipped", reason="no_audio")

    transcriber = ctx.transcriber
    if transcriber is None:
        from vidcleaner.pipeline.stt import get_transcriber  # noqa: PLC0415

        transcriber = get_transcriber(ctx.settings, mode="drift")

    request = TranscribeRequest(
        audio_path=ctx.ws.audio_wav,
        windows=[p.span for p in plans],
        model=model,
        language=lang.to_iso639_1(ctx.settings.preferred_language),
        beam_size=ctx.settings.beam_size,
        vad_filter=ctx.settings.vad_filter,
        cpu_threads=ctx.settings.cpu_threads,
        # Same clock discipline as the main pass: probe spans are container time
        # and whisper_backend shifts them onto audio.wav's clock.
        time_offset_s=probe.source_audio.start_time,
        model_cache_dir=ctx.deploy.config_dir / "models",
        duration_s=probe.duration,
        # Alignment buys nothing here. A median over many anchors is already
        # robust to the 100-400 ms late bias whisperX exists to remove, and
        # aligning would roughly double the cost of a check that runs every job.
        align=False,
    )

    started = time.monotonic()
    try:
        transcript = transcriber.transcribe(request)
    except Exception as exc:  # noqa: BLE001 - never fail a job over a measurement
        ctx.log.warning("drift.failed", error=str(exc), model=model)
        return DriftResult(checked=False, model=model, action="skipped", reason="stt_failed")

    words = list(transcript.words)
    observations = [
        align_probe(plan, [w for w in words if plan.span.start <= w.start <= plan.span.end])
        for plan in plans
    ]
    return build_result(observations, plans, model=model, elapsed_s=time.monotonic() - started)
