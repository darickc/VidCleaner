"""Stage 5: turn subtitle hits and STT words into mute ranges (PLAN.md §7).

``detect`` is a **pure function of already-loaded values** -- no whisper
objects, no ffmpeg, no database -- so the whole detector is unit-testable
without the STT stack installed. ``run`` is the thin wrapper that reads
``subs.json`` and ``transcript.json`` and writes ``detections.json``.

Two orderings in here are load-bearing and each has a regression test:

**Censored tokens are resolved before fuzzy matching.** Whisper self-censors, and
``fuzz.ratio("f***", "fuck")`` is only about 50, so a fuzzy-first implementation
loses the pairing entirely.

**Guards run before padding.** A guard applied after padding would flag a
legitimate 2.9 s span as suspicious purely because 200 ms of padding pushed it
over the 3 s limit.

One extension beyond §7, logged in §14: within the windows we already
transcribed, the matcher is also run over the STT tokens themselves. Whisper
frequently hears a word the subtitles sanitised (fan subs, or subs cut for TV),
and since the audio is already transcribed this costs nothing.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz

from vidcleaner.matching.compiler import Matcher
from vidcleaner.matching.normalize import (
    Token,
    censored_candidates,
    fold,
    join_tokens,
    span_to_tokens,
    tokenize,
)
from vidcleaner.pipeline.artifacts import (
    Detection,
    DetectionResult,
    SubtitleCue,
    SubtitleHit,
    SubtitlesResult,
    TimeRange,
    Transcript,
    WordCount,
    merge_ranges,
)
from vidcleaner.pipeline.workspace import Workspace

NAME = "detect"

__all__ = ["NAME", "DetectOptions", "detect", "load", "run", "tokens_from_transcript"]

#: §7's fuzzy threshold for choosing the *timing* token. Never used to expand
#: the word list -- every comparison target is an authored form.
FUZZ_THRESHOLD = 85.0
#: §7's guards.
MAX_RANGE_S = 3.0
MAX_CUE_OFFSET_S = 2.5
#: §7's subtitle-only fallback padding around the proportional span.
SUBTITLE_FALLBACK_PAD_S = 0.4
#: Search radius around a cue for its timing token.
WINDOW_SLACK_S = 1.0
#: `asetnsamples=n=240` gives 5 ms granularity, so anything shorter is noise.
MIN_MUTE_S = 0.020

CONFIDENCE_EXACT = 0.95
CONFIDENCE_CENSORED = 0.8
CONFIDENCE_SUBTITLE_ONLY = 0.3
CONFIDENCE_CENSORED_ALONE = 0.6

CENSORED_CANONICAL = "<censored>"


@dataclass(frozen=True, slots=True)
class DetectOptions:
    pad_pre_ms: int = 80
    pad_post_ms: int = 120
    merge_gap_ms: int = 250
    mute_censored_tokens: bool = True
    max_range_s: float = MAX_RANGE_S
    max_cue_offset_s: float = MAX_CUE_OFFSET_S
    fuzz_threshold: float = FUZZ_THRESHOLD

    @classmethod
    def from_profile(cls, profile, settings=None) -> DetectOptions:
        return cls(
            pad_pre_ms=profile.pad_pre_ms,
            pad_post_ms=profile.pad_post_ms,
            merge_gap_ms=getattr(profile, "merge_gap_ms", 250),
            mute_censored_tokens=getattr(profile, "mute_censored_tokens", True),
        )


def tokens_from_transcript(transcript: Transcript, never_match=()) -> list[Token]:
    """Flatten a transcript into normalized, timed tokens."""
    out: list[Token] = []
    for index, word in enumerate(transcript.words):
        out.append(
            tokenize(
                word.word,
                index=index,
                never_match=never_match,
                start_s=word.start,
                end_s=word.end,
                prob=word.probability,
            )
        )
    return out


# ------------------------------------------------------------- token lookup


@dataclass(frozen=True, slots=True)
class _Index:
    tokens: tuple[Token, ...]
    starts: tuple[float, ...]

    @classmethod
    def build(cls, tokens: Sequence[Token]) -> _Index:
        timed = tuple(t for t in tokens if t.start_s is not None and t.end_s is not None)
        return cls(timed, tuple(t.start_s or 0.0 for t in timed))

    def between(self, start: float, end: float) -> list[Token]:
        first = bisect_left(self.starts, start)
        out: list[Token] = []
        for token in self.tokens[first:]:
            if (token.start_s or 0.0) > end:
                break
            out.append(token)
        return out


def _score(token: Token, hit_targets: frozenset[str]) -> float:
    if token.fold in hit_targets:
        return 100.0
    return max((fuzz.ratio(token.fold, target) for target in hit_targets), default=0.0)


def _targets(matcher: Matcher, canonical: str, raw: str) -> frozenset[str]:
    """Compare against the entry's whole form table, not the matched string.

    ``fuzz.ratio("fuck", "fucking")`` is 72.7, so a subtitle hit on ``fuck``
    paired against Whisper's ``fucking`` would otherwise fall below the
    threshold and lose its timing. Every target here is already an authored
    form, so this widens *timing* recall without widening the word list.
    """
    forms = {fold(f) for f in matcher.forms_of(canonical)}
    forms.add(fold(raw))
    return frozenset(f for f in forms if f)


def _pick_token(
    candidates: Sequence[Token],
    *,
    matcher: Matcher,
    canonical: str,
    raw: str,
    expected_mid: float,
    cue_mid: float | None,
    opts: DetectOptions,
    claimed: set[int] | None = None,
) -> tuple[Token | None, float, str]:
    """Choose the STT token that supplies this hit's timing.

    Returns ``(token, confidence, kind)`` where ``kind`` is ``exact``, ``fuzzy``
    or ``censored``.
    """
    targets = _targets(matcher, canonical, raw)
    best: tuple[float, float, Token, str] | None = None

    for token in candidates:
        if token.kind == "empty" or token.fold in matcher.never_match:
            continue
        if claimed is not None and token.index in claimed:
            continue
        token_mid = ((token.start_s or 0.0) + (token.end_s or 0.0)) / 2.0
        if cue_mid is not None and abs(token_mid - cue_mid) > opts.max_cue_offset_s:
            continue

        # Censored first: `fuzz.ratio("f***", "fuck")` is ~50 and would lose it.
        if token.censor is not None:
            resolved = censored_candidates(token.censor, matcher.by_letter_len, matcher.by_letter)
            if any(c.canonical == canonical for c in resolved):
                score, kind = 95.0, "censored"
            else:
                continue
        else:
            score = _score(token, targets)
            if score < opts.fuzz_threshold:
                continue
            kind = "exact" if score >= 100.0 else "fuzzy"

        distance = abs(token_mid - expected_mid)
        key = (score, -distance, token, kind)
        if best is None or (key[0], key[1]) > (best[0], best[1]):
            best = key

    if best is None:
        return None, 0.0, "none"

    score, _, token, kind = best
    if kind == "censored":
        confidence = CONFIDENCE_CENSORED
    elif kind == "exact":
        confidence = CONFIDENCE_EXACT
    else:
        confidence = round(score / 100.0 * 0.9, 3)
    return token, confidence, kind


# ------------------------------------------------------------------- guards


def _apply_guards(
    detection: Detection,
    cue: SubtitleCue | None,
    offset_s: float,
    opts: DetectOptions,
    fallback: TimeRange | None = None,
) -> Detection:
    """§7's sanity checks, applied to the **unpadded** span.

    ``fallback`` is the hit's drift-corrected proportional span, carried over
    from the windowed pass so that a rejected token falls back to what the
    subtitle itself implied. With no cue -- an STT-only or full-mode hit --
    there is nothing to fall back to, so an implausibly long span is clamped to
    one second instead.
    """
    start, end = detection.start_s, detection.end_s
    reason: str | None = None

    if end - start > opts.max_range_s:
        reason = f"span {end - start:.2f}s exceeds {opts.max_range_s:g}s"
    elif cue is not None:
        cue_mid = (cue.start + cue.end) / 2.0 + offset_s
        drift = abs((start + end) / 2.0 - cue_mid)
        if drift > opts.max_cue_offset_s:
            reason = f"{drift:.2f}s from its cue midpoint"

    if reason is None:
        return detection

    if fallback is not None:
        start, end = fallback.start, fallback.end
    elif cue is not None:
        start = max(0.0, cue.start + offset_s - SUBTITLE_FALLBACK_PAD_S)
        end = cue.end + offset_s + SUBTITLE_FALLBACK_PAD_S
    else:
        end = start + 1.0

    return detection.model_copy(
        update={
            "start_s": start,
            "end_s": end,
            "suspicious": True,
            "suspicious_reason": reason,
        }
    )


def _pad(detection: Detection, duration_s: float, opts: DetectOptions) -> Detection:
    start = max(0.0, detection.start_s - opts.pad_pre_ms / 1000.0)
    end = detection.end_s + opts.pad_post_ms / 1000.0
    if duration_s > 0:
        end = min(duration_s, end)
    if end - start < MIN_MUTE_S:
        end = min(duration_s, start + MIN_MUTE_S) if duration_s > 0 else start + MIN_MUTE_S
    return detection.model_copy(update={"mute_start_s": start, "mute_end_s": end})


# ---------------------------------------------------------------- windowed


def _phrase_span(
    hit: SubtitleHit,
    candidates: Sequence[Token],
    *,
    matcher: Matcher,
    expected_mid: float,
    cue_mid: float | None,
    opts: DetectOptions,
    claimed: set[int] | None = None,
) -> tuple[float, float, float, str] | None:
    """Locate a phrase by its first and last word, honouring ``focus``."""
    entry = matcher.entry_for(hit.word_canonical)
    words = [w for w in fold(hit.word_raw).split() if w]
    if entry is None or len(words) < 2:
        return None

    wanted = list(entry.focus) if entry.focus else [words[0], words[-1]]
    found: list[Token] = []
    confidences: list[float] = []
    for word in wanted:
        token, confidence, kind = _pick_token(
            candidates,
            matcher=matcher,
            canonical=hit.word_canonical,
            raw=word,
            expected_mid=expected_mid,
            cue_mid=cue_mid,
            opts=opts,
            claimed=claimed,
        )
        if token is None:
            continue
        if claimed is not None:
            claimed.add(token.index)
        found.append(token)
        confidences.append(confidence)
        del kind

    if not found:
        return None
    start = min(t.start_s or 0.0 for t in found)
    end = max(t.end_s or 0.0 for t in found)
    confidence = min(confidences)
    kind = "both" if len(found) == len(wanted) else "partial"
    return start, end, confidence, kind


def _windowed(
    *,
    matcher: Matcher,
    cues: Sequence[SubtitleCue],
    hits: Sequence[SubtitleHit],
    index: _Index,
    offset_s: float,
    opts: DetectOptions,
) -> tuple[list[Detection], list[TimeRange | None]]:
    by_index = {cue.index: cue for cue in cues}
    out: list[Detection] = []
    fallbacks: list[TimeRange | None] = []
    claimed: set[int] = set()

    for hit in hits:
        cue = by_index.get(hit.cue_index)
        expected_start = hit.start + offset_s
        expected_end = hit.end + offset_s
        expected_mid = (expected_start + expected_end) / 2.0
        cue_mid = ((cue.start + cue.end) / 2.0 + offset_s) if cue else None

        search_start = (cue.start if cue else hit.start) + offset_s - WINDOW_SLACK_S
        search_end = (cue.end if cue else hit.end) + offset_s + WINDOW_SLACK_S
        candidates = index.between(search_start, search_end)

        entry = matcher.entry_for(hit.word_canonical)
        result = None
        if entry is not None and entry.is_phrase:
            result = _phrase_span(
                hit,
                candidates,
                matcher=matcher,
                expected_mid=expected_mid,
                cue_mid=cue_mid,
                opts=opts,
                claimed=claimed,
            )

        if result is not None:
            start, end, confidence, kind = result
            suspicious_reason = None if kind == "both" else "only part of the phrase was located"
            detection = Detection(
                word_raw=hit.word_raw,
                word_canonical=hit.word_canonical,
                category=hit.category,
                start_s=start,
                end_s=end,
                mute_start_s=start,
                mute_end_s=end,
                source="both",
                confidence=confidence,
                suspicious=kind != "both",
                suspicious_reason=suspicious_reason,
                subtitle_cue_idx=hit.cue_index,
            )
        else:
            token, confidence, kind = _pick_token(
                candidates,
                matcher=matcher,
                canonical=hit.word_canonical,
                raw=hit.word_raw,
                expected_mid=expected_mid,
                cue_mid=cue_mid,
                opts=opts,
                claimed=claimed,
            )
            if token is not None:
                claimed.add(token.index)
                detection = Detection(
                    word_raw=hit.word_raw,
                    word_canonical=hit.word_canonical,
                    category=hit.category,
                    start_s=token.start_s or 0.0,
                    end_s=token.end_s or 0.0,
                    mute_start_s=token.start_s or 0.0,
                    mute_end_s=token.end_s or 0.0,
                    source="both",
                    confidence=confidence,
                    subtitle_cue_idx=hit.cue_index,
                )
            else:
                # §7: no STT token found -> mute the drift-corrected
                # proportional span, flagged for review.
                detection = Detection(
                    word_raw=hit.word_raw,
                    word_canonical=hit.word_canonical,
                    category=hit.category,
                    start_s=max(0.0, expected_start - SUBTITLE_FALLBACK_PAD_S),
                    end_s=expected_end + SUBTITLE_FALLBACK_PAD_S,
                    mute_start_s=0.0,
                    mute_end_s=0.0,
                    source="subtitle",
                    confidence=CONFIDENCE_SUBTITLE_ONLY,
                    suspicious=True,
                    suspicious_reason="no STT token matched in the window",
                    subtitle_cue_idx=hit.cue_index,
                )
        if matcher.is_suppressed(hit.word_canonical, cue.text if cue else hit.word_raw):
            detection = detection.model_copy(update={"whitelisted": True, "muted": False})
        out.append(detection)
        fallbacks.append(
            TimeRange(
                start=max(0.0, expected_start - SUBTITLE_FALLBACK_PAD_S),
                end=expected_end + SUBTITLE_FALLBACK_PAD_S,
            )
        )
    return out, fallbacks


def _same_finding(existing: Detection, canonical: str, start: float, end: float) -> bool:
    """Is this STT match already covered by an existing detection?

    Overlap alone would be too aggressive: a subtitle fallback span can be a
    whole cue wide and would swallow a genuinely different word spoken beside
    it. Overlap *plus* a related canonical is the right test -- it collapses a
    duplicate ``fuck``, and also the bare ``god`` that a partially located
    ``god damn`` phrase would otherwise double-count.
    """
    if existing.end_s < start or end < existing.start_s:
        return False
    if existing.word_canonical == canonical:
        return True
    return canonical in existing.word_canonical.split() or (
        existing.word_canonical in canonical.split()
    )


def _stt_only(
    *,
    matcher: Matcher,
    tokens: Sequence[Token],
    existing: Sequence[Detection],
    opts: DetectOptions,
) -> list[Detection]:
    """Matches Whisper heard that the subtitles did not name.

    The audio in these windows is already transcribed, so this is free -- and it
    closes an obvious hole for sanitised or TV-cut subtitle tracks.
    """
    # Empty-norm (punctuation-only) tokens are retained deliberately: they are
    # zero width, but `join_tokens` turns them into the hard break that stops a
    # phrase spanning a sentence boundary.
    timed = [t for t in tokens if t.start_s is not None]
    if not any(t.norm for t in timed):
        return []
    joined = join_tokens(timed)
    out: list[Detection] = []
    for match in matcher.finditer(joined.text):
        if matcher.suppressed(match, joined.text):
            continue
        try:
            first, last = span_to_tokens(joined, match.start, match.end)
        except ValueError:
            continue
        start = timed[first].start_s or 0.0
        end = timed[last].end_s or 0.0
        if any(_same_finding(d, match.canonical, start, end) for d in existing):
            continue
        probs = [t.prob for t in timed[first : last + 1] if t.prob is not None]
        out.append(
            Detection(
                word_raw=match.raw,
                word_canonical=match.canonical,
                category=match.category,
                start_s=start,
                end_s=end,
                mute_start_s=start,
                mute_end_s=end,
                source="stt",
                confidence=round(sum(probs) / len(probs), 3) if probs else None,
            )
        )
    return out


def _standalone_censored(
    *,
    matcher: Matcher,
    tokens: Sequence[Token],
    existing: Sequence[Detection],
    opts: DetectOptions,
) -> list[Detection]:
    """Masked tokens with no subtitle evidence (§7: `strong`, 0.6, suspicious)."""
    out: list[Detection] = []
    for token in tokens:
        if token.censor is None or token.censor.style != "masked":
            continue
        start, end = token.start_s, token.end_s
        if start is None or end is None:
            continue
        if any(d.start_s < end and start < d.end_s for d in existing):
            continue
        resolved = censored_candidates(token.censor, matcher.by_letter_len, matcher.by_letter)
        canonicals = {c.canonical for c in resolved}
        canonical = canonicals.pop() if len(canonicals) == 1 else CENSORED_CANONICAL
        if canonical == CENSORED_CANONICAL and not resolved:
            continue
        out.append(
            Detection(
                word_raw=token.raw,
                word_canonical=canonical,
                category="strong",
                start_s=start,
                end_s=end,
                mute_start_s=start,
                mute_end_s=end,
                source="stt",
                confidence=CONFIDENCE_CENSORED_ALONE,
                muted=opts.mute_censored_tokens,
                suspicious=True,
                suspicious_reason="self-censored token with no subtitle evidence",
            )
        )
    return out


def _full(*, matcher: Matcher, tokens: Sequence[Token], opts: DetectOptions) -> list[Detection]:
    """Whole-transcript matching. Used by M2's full and audit passes.

    Built and tested in M1 so the seam is real: ``join_tokens`` uses a single
    space and never a newline, which makes phrase matching behave identically
    here and in the per-cue path -- exactly the property the audit pass needs in
    order to be comparable to a windowed run.
    """
    return _stt_only(matcher=matcher, tokens=tokens, existing=(), opts=opts)


# -------------------------------------------------------------------- public


def _counts(detections: Sequence[Detection]) -> list[WordCount]:
    grouped: dict[tuple[str, str], dict] = {}
    for detection in detections:
        key = (detection.word_canonical, detection.category)
        bucket = grouped.setdefault(key, {"total": 0, "muted": 0, "suspicious": 0, "sources": {}})
        bucket["total"] += 1
        bucket["muted"] += int(detection.muted and not detection.whitelisted)
        bucket["suspicious"] += int(detection.suspicious)
        bucket["sources"][detection.source] = bucket["sources"].get(detection.source, 0) + 1

    rows = [
        WordCount(
            word_canonical=canonical,
            category=category,
            total=data["total"],
            muted=data["muted"],
            suspicious=data["suspicious"],
            sources=data["sources"],
        )
        for (canonical, category), data in grouped.items()
    ]
    rows.sort(key=lambda r: (-r.total, r.word_canonical))
    return rows


def detect(
    *,
    matcher: Matcher,
    cues: Sequence[SubtitleCue] | None,
    tokens: Sequence[Token],
    mode: Literal["windowed", "full", "audit"] = "windowed",
    hits: Sequence[SubtitleHit] = (),
    drift_offset_s: float = 0.0,
    duration_s: float = 0.0,
    opts: DetectOptions | None = None,
) -> DetectionResult:
    """Pure: subtitle hits plus STT tokens in, detections and mute ranges out."""
    opts = opts or DetectOptions()
    index = _Index.build(tokens)

    if mode == "windowed" and cues is not None:
        detections, fallbacks = _windowed(
            matcher=matcher,
            cues=cues,
            hits=hits,
            index=index,
            offset_s=drift_offset_s,
            opts=opts,
        )
        extra = _stt_only(matcher=matcher, tokens=index.tokens, existing=detections, opts=opts)
    else:
        detections = _full(matcher=matcher, tokens=index.tokens, opts=opts)
        fallbacks = [None] * len(detections)
        extra = []

    extra += _standalone_censored(
        matcher=matcher,
        tokens=index.tokens,
        existing=[*detections, *extra],
        opts=opts,
    )
    detections = [*detections, *extra]
    fallbacks = [*fallbacks, *([None] * len(extra))]

    by_cue = {cue.index: cue for cue in (cues or [])}
    finished: list[Detection] = []
    for detection, fallback in zip(detections, fallbacks, strict=True):
        cue = (
            by_cue.get(detection.subtitle_cue_idx)
            if detection.subtitle_cue_idx is not None
            else None
        )
        guarded = _apply_guards(detection, cue, drift_offset_s, opts, fallback)
        finished.append(_pad(guarded, duration_s, opts))

    finished.sort(key=lambda d: (d.start_s, d.end_s, d.word_canonical))

    mutable = [d for d in finished if d.muted and not d.whitelisted]
    ranges = merge_ranges(
        [TimeRange(start=d.mute_start_s, end=d.mute_end_s) for d in mutable],
        opts.merge_gap_ms / 1000.0,
    )

    stats = {
        "detections": len(finished),
        "muted": len(mutable),
        "suspicious": sum(1 for d in finished if d.suspicious),
        "whitelisted": sum(1 for d in finished if d.whitelisted),
        "ranges": len(ranges),
    }
    for detection in finished:
        stats[detection.source] = stats.get(detection.source, 0) + 1

    return DetectionResult(
        profile_hash=matcher.profile_hash,
        detections=finished,
        mute_ranges=ranges,
        counts=_counts(finished),
        total_muted_s=round(sum(r.duration for r in ranges), 3),
        stats=stats,
    )


def run(ctx) -> None:
    from vidcleaner.pipeline.artifacts import ProbeResult  # noqa: PLC0415

    probe = ProbeResult.read(ctx.ws.probe_json)
    subs = SubtitlesResult.read(ctx.ws.subs_json)
    transcript = (
        Transcript.read(ctx.ws.transcript_json)
        if ctx.ws.transcript_json.is_file()
        else Transcript()
    )

    matcher = ctx.matcher
    if matcher is None:
        from vidcleaner.matching.compiler import build_matcher  # noqa: PLC0415

        matcher = build_matcher()
        ctx.matcher = matcher

    tokens = tokens_from_transcript(transcript, matcher.never_match)
    mode = "windowed" if subs.cues else "full"
    result = detect(
        matcher=matcher,
        cues=subs.cues,
        tokens=tokens,
        mode=mode,  # type: ignore[arg-type]
        hits=subs.hits,
        drift_offset_s=subs.offset_s,
        duration_s=probe.duration,
        opts=DetectOptions.from_profile(ctx.spec.profile),
    )

    ctx.log.info(
        "detect.done",
        mode=mode,
        tokens=len(tokens),
        **{k: v for k, v in result.stats.items()},
        muted_s=result.total_muted_s,
    )
    result.write(ctx.ws.detections_json)


def load(ws: Workspace) -> DetectionResult:
    return DetectionResult.read(ws.detections_json)
