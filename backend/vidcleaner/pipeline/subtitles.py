"""Stage 3: pick a subtitle track, find candidate windows, redact text (PLAN.md §6 step 3, §7).

Subtitles do two jobs. They narrow which parts of the audio need STT at all --
without that, a feature film needs a full-file pass -- and they name the word,
so STT only has to supply the *timing*.

Redaction shares the *same* ``Matcher`` instance as detection, so what gets
masked is exactly what gets muted, including whitelist scope. Two constraints
shape the implementation:

* ``pysubs2``'s ``SSAEvent.plaintext`` **setter destroys ASS override tags**
  (it substitutes out ``{...}`` blocks), so redaction splices into
  ``SSAEvent.text`` through an offset map instead.
* Only streams in a language we actually have a word list for are redacted.
  The M1 test media carries **61** text subtitle streams; extracting and
  redacting 60 of them for languages with no word list is pure waste.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pysubs2

from vidcleaner.matching.compiler import Matcher, mask_text
from vidcleaner.pipeline import lang
from vidcleaner.pipeline.artifacts import (
    ProbeResult,
    SubtitleCue,
    SubtitleHit,
    SubtitleSource,
    SubtitlesResult,
    SubtitleStreamInfo,
    TimeRange,
    merge_ranges,
)
from vidcleaner.pipeline.workspace import Workspace

NAME = "subtitles"

__all__ = [
    "REDACTABLE_LANGUAGES",
    "no_subtitles",
    "subtitle_candidates",
    "RedactionStats",
    "choose_subtitle_source",
    "cue_windows",
    "find_hits",
    "load",
    "parse_cues",
    "redact_file",
    "redact_line",
    "run",
]

#: Languages we ship word lists for. Multi-language lists are a §11 "later" item.
REDACTABLE_LANGUAGES = frozenset({"eng"})

#: §6 step 3: pad each cue by 1.5 s, then merge windows less than 2 s apart.
WINDOW_PAD_S = 1.5
WINDOW_MERGE_GAP_S = 2.0

SIDECAR_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt")

_SUB_EXTRACT_FORMAT = {
    "ass": ("ass", ".ass"),
    "ssa": ("ass", ".ass"),
    "webvtt": ("webvtt", ".vtt"),
}
#: ASS override blocks and escapes: never scanned, always preserved.
_MARKUP_RE = re.compile(r"(\{[^}]*\}|\\[Nnh])")
#: Raw sequences that render as a single visible character.
_ESCAPE_WIDTH = {"\\N": "\n", "\\n": "\n", "\\h": " "}


@dataclass(frozen=True, slots=True)
class RedactionStats:
    path: Path
    cues_total: int = 0
    cues_changed: int = 0
    hits: int = 0
    tags_dropped: int = 0


# ------------------------------------------------------------ source choice


def find_sidecars(source: Path) -> list[Path]:
    """Sibling subtitle files sharing the source's stem."""
    stem = source.stem
    out: list[Path] = []
    try:
        candidates = sorted(source.parent.iterdir())
    except OSError:
        return out
    for candidate in candidates:
        if not candidate.is_file() or candidate.suffix.lower() not in SIDECAR_SUFFIXES:
            continue
        if candidate.stem == stem or candidate.stem.startswith(f"{stem}."):
            out.append(candidate)
    return out


def _sidecar_language(sidecar: Path, source_stem: str) -> str | None:
    """``Movie.eng.srt`` -> ``eng``; ``Movie.srt`` -> unknown."""
    extra = sidecar.stem[len(source_stem) :].strip(".")
    for part in extra.split("."):
        if part and lang.to_iso639_1(part) is not None:
            return lang.normalize_tag(part)
    return None


def subtitle_candidates(
    probe: ProbeResult,
    *,
    preferred_language: str | None,
    sidecars: Sequence[Path] = (),
) -> list[SubtitleSource]:
    """§6 step 3's precedence, as a list rather than a single answer.

    Every candidate, best first, so the stage can move on when one turns out to be
    unparseable. A corrupt ``Movie.srt`` beside a file with perfectly good embedded
    English subtitles used to fail the entire job, because §6's precedence prefers
    the sidecar and nothing looked past it.
    """
    source_stem = Path(probe.path).stem
    out: list[SubtitleSource] = []

    preferred_sidecars = [
        s for s in sidecars if lang.matches(_sidecar_language(s, source_stem), preferred_language)
    ]
    for sidecar in preferred_sidecars or list(sidecars):
        detected = _sidecar_language(sidecar, source_stem)
        if preferred_sidecars or detected is None:
            out.append(
                SubtitleSource(
                    kind="sidecar",
                    path=str(sidecar),
                    codec_name=sidecar.suffix.lstrip(".").lower(),
                    language=detected or preferred_language,
                    reason=(
                        "sidecar_preferred_language" if preferred_sidecars else "sidecar_untagged"
                    ),
                )
            )

    text_streams = probe.text_subtitles
    seen: set[int] = set()

    def add(stream: SubtitleStreamInfo, reason: str) -> None:
        if stream.typed_index in seen:
            return
        seen.add(stream.typed_index)
        out.append(_embedded(stream, reason))

    for stream in text_streams:
        if lang.matches(stream.language, preferred_language) and not stream.is_forced:
            add(stream, "embedded_preferred_language")
    for stream in text_streams:
        if lang.matches(stream.language, preferred_language):
            add(stream, "embedded_preferred_language_forced")
    for stream in text_streams:
        if not stream.is_forced:
            add(stream, "embedded_any_text")
    for stream in text_streams:
        add(stream, "embedded_forced_only")

    return out


def no_subtitles(probe: ProbeResult) -> SubtitleSource:
    reason = "no_text_subtitles_only_bitmap" if probe.subtitles else "no_subtitles"
    return SubtitleSource(kind="none", reason=reason)


def choose_subtitle_source(
    probe: ProbeResult,
    *,
    preferred_language: str | None,
    sidecars: Sequence[Path] = (),
) -> SubtitleSource:
    """The best candidate, or ``kind="none"``. See :func:`subtitle_candidates`."""
    candidates = subtitle_candidates(
        probe, preferred_language=preferred_language, sidecars=sidecars
    )
    return candidates[0] if candidates else no_subtitles(probe)


def _embedded(stream: SubtitleStreamInfo, reason: str) -> SubtitleSource:
    return SubtitleSource(
        kind="embedded",
        stream_typed_index=stream.typed_index,
        codec_name=stream.codec_name,
        language=stream.language,
        reason=reason,
    )


def redactable_streams(probe: ProbeResult) -> list[int]:
    """Text streams whose language we have a word list for.

    A stream with no language tag is skipped: redacting it would mean guessing,
    and the M1 media has three such streams (Chinese, titled but untagged).
    """
    return [
        s.typed_index
        for s in probe.text_subtitles
        if s.language is not None
        and any(lang.matches(s.language, known) for known in REDACTABLE_LANGUAGES)
    ]


# ------------------------------------------------------------------ parsing


def redactable_sidecars(
    source: Path, sidecars: Sequence[Path], *, preferred_language: str | None = None
) -> list[Path]:
    """Sidecar files we should mask, given the word lists we ship.

    Deliberately asymmetric with :func:`redactable_streams`, which skips a stream
    with no ``language`` tag: there, guessing is unnecessary (the M1 media has 61
    text streams, one of them English) and wrong guesses are cheap to avoid. A
    sidecar is the opposite case -- ``Movie.srt`` with no language in its name is
    the *usual* shape, and it is almost always the primary language, so skipping
    untagged files would mean the commonest sidecar layout never got redacted.
    The asymmetry is safe because redaction only masks what the matcher matches:
    running an English word list over a Spanish subtitle finds nothing.

    A sidecar tagged with a language we have no list for is excluded, since
    nothing could match it and rewriting it would be pure risk.
    """
    known = {*REDACTABLE_LANGUAGES, *([preferred_language] if preferred_language else [])}
    out: list[Path] = []
    for sidecar in sidecars:
        tag = _sidecar_language(sidecar, source.stem)
        if tag is None or any(lang.matches(tag, other) for other in known):
            out.append(sidecar)
    return out


def parse_cues(path: Path) -> list[SubtitleCue]:
    """Parse to visible text, ASS override tags stripped."""
    try:
        subs = pysubs2.load(str(path))
    except Exception as exc:  # pysubs2 raises a variety of parse errors
        raise ValueError(f"cannot parse subtitles at {path}: {exc}") from exc

    cues: list[SubtitleCue] = []
    for index, event in enumerate(subs):
        if event.is_comment or event.is_drawing:
            continue
        text = event.plaintext.strip()
        if not text:
            continue
        cues.append(
            SubtitleCue(
                index=index,
                start=event.start / 1000.0,
                end=event.end / 1000.0,
                text=text,
            )
        )
    return cues


def find_hits(cues: Iterable[SubtitleCue], matcher: Matcher) -> list[SubtitleHit]:
    """Word-list matches per cue, with a proportional time span for each.

    The span is derived from character offsets rather than word counts, which
    keeps a match near the end of a long cue near the end of its time range.
    """
    hits: list[SubtitleHit] = []
    for cue in cues:
        length = max(1, len(cue.text))
        duration = cue.duration
        for match in matcher.finditer(cue.text):
            hits.append(
                SubtitleHit(
                    cue_index=cue.index,
                    start=cue.start + duration * (match.start / length),
                    end=cue.start + duration * (match.end / length),
                    word_raw=match.raw,
                    word_canonical=match.canonical,
                    category=match.category,
                    char_start=match.start,
                    char_end=match.end,
                )
            )
    return hits


def cue_windows(
    cues: Sequence[SubtitleCue],
    hits: Sequence[SubtitleHit],
    *,
    pad_s: float = WINDOW_PAD_S,
    merge_gap_s: float = WINDOW_MERGE_GAP_S,
    duration: float | None = None,
    offset_s: float = 0.0,
) -> list[TimeRange]:
    """STT candidate windows around every cue that contains a hit.

    Padding is applied to the *cue* bounds rather than the hit's proportional
    span: the proportional estimate can be off within the cue, and a window that
    is too narrow loses the word entirely.
    """
    by_index = {cue.index: cue for cue in cues}
    raw: list[TimeRange] = []
    for hit in hits:
        cue = by_index.get(hit.cue_index)
        start = (cue.start if cue else hit.start) + offset_s - pad_s
        end = (cue.end if cue else hit.end) + offset_s + pad_s
        raw.append(
            TimeRange(
                start=max(0.0, start),
                end=min(duration, end) if duration else end,
            )
        )
    return merge_ranges(raw, merge_gap_s)


# ----------------------------------------------------------------- redaction


def _visible_map(raw: str) -> tuple[str, list[int], list[int]]:
    """Split markup out of ``SSAEvent.text``, returning the visible text and offsets.

    ``vis_to_raw[i]`` is where visible character ``i`` starts in ``raw``, and
    ``raw_width[i]`` is how many raw characters it occupies (2 for ``\\N``).
    """
    visible_parts: list[str] = []
    vis_to_raw: list[int] = []
    raw_width: list[int] = []
    cursor = 0
    for token in _MARKUP_RE.split(raw):
        if not token:
            continue
        if _MARKUP_RE.fullmatch(token):
            if (rendered := _ESCAPE_WIDTH.get(token)) is not None:
                visible_parts.append(rendered)
                vis_to_raw.append(cursor)
                raw_width.append(len(token))
            cursor += len(token)
            continue
        for offset, char in enumerate(token):
            visible_parts.append(char)
            vis_to_raw.append(cursor + offset)
            raw_width.append(1)
        cursor += len(token)
    return "".join(visible_parts), vis_to_raw, raw_width


def redact_line(raw: str, matcher: Matcher, *, mask_char: str = "*") -> tuple[str, int, int]:
    """Mask matches in one event's raw text. Returns ``(text, hits, tags_dropped)``.

    Matching runs on the *visible* text with markup removed, so a word split
    across a tag boundary (``f{\\i1}uck``) is still found -- and, since the mask
    replaces the whole raw span, the override block inside it is necessarily
    lost. That is the right trade for a profanity filter, but it is counted in
    ``tags_dropped`` rather than happening silently.

    Markup *outside* a match is untouched, and replacements are spliced back to
    front so earlier offsets stay valid.
    """
    visible, vis_to_raw, raw_width = _visible_map(raw)
    matches = [m for m in matcher.finditer(visible) if not matcher.suppressed(m, visible)]
    if not matches:
        return raw, 0, 0

    out = raw
    dropped = 0
    for match in reversed(matches):
        begin = vis_to_raw[match.start]
        last = match.end - 1
        finish = vis_to_raw[last] + raw_width[last]
        replaced = out[begin:finish]
        # Any override block inside the span dies with it; only count real
        # `{...}` blocks, not the \N escapes that map to a visible character.
        dropped += len(re.findall(r"\{[^}]*\}", replaced))
        out = out[:begin] + mask_text(match.raw, mask_char) + out[finish:]
    return out, len(matches), dropped


def _load_best_candidate(
    ctx, probe: ProbeResult, sidecars: Sequence[Path]
) -> tuple[SubtitleSource, list[SubtitleCue]]:
    """The first candidate that actually parses, with its cues.

    A candidate that cannot be read is a warning, not a failure: the file may have
    four other subtitle tracks, and having no cues at all merely means the job
    falls back to a full-file pass. Failing the job would be a strictly worse
    outcome than either.
    """
    candidates = subtitle_candidates(
        probe, preferred_language=ctx.settings.preferred_language, sidecars=sidecars
    )
    for candidate in candidates:
        try:
            if candidate.kind == "sidecar" and candidate.path:
                return candidate, parse_cues(Path(candidate.path))
            if candidate.kind == "embedded" and candidate.stream_typed_index is not None:
                extracted = _extract_stream(ctx, probe, candidate.stream_typed_index)
                return candidate.model_copy(update={"path": str(extracted)}), parse_cues(extracted)
        except Exception as exc:  # noqa: BLE001 - any unreadable candidate is skippable
            ctx.log.warning(
                "subtitles.candidate_unusable",
                kind=candidate.kind,
                path=candidate.path,
                stream=candidate.stream_typed_index,
                error=str(exc)[:200],
            )
    return no_subtitles(probe), []


def redact_file(
    source: Path, dest: Path, matcher: Matcher, *, mask_char: str = "*"
) -> RedactionStats:
    """Redact a subtitle file, preserving timing, styles and markup."""
    subs = pysubs2.load(str(source))
    changed = hits = dropped = 0
    for event in subs:
        if event.is_comment or event.is_drawing or not event.text:
            continue
        text, count, lost = redact_line(event.text, matcher, mask_char=mask_char)
        if count:
            event.text = text
            changed += 1
            hits += count
            dropped += lost
    dest.parent.mkdir(parents=True, exist_ok=True)
    subs.save(str(dest))
    return RedactionStats(dest, len(subs), changed, hits, dropped)


# --------------------------------------------------------------------- stage


def _extract_stream(ctx, probe: ProbeResult, typed_index: int) -> Path:
    stream = next(s for s in probe.subtitles if s.typed_index == typed_index)
    codec, suffix = _SUB_EXTRACT_FORMAT.get(stream.codec_name, ("srt", ".srt"))
    target = ctx.ws.subs_dir / f"in_{typed_index}{suffix}"
    ctx.runner.run(
        [
            "-i",
            probe.path,
            "-map",
            f"0:s:{typed_index}",
            "-c:s",
            codec,
            str(target),
        ],
        label=f"extract-sub-{typed_index}",
        timeout=600,
    )
    return target


def run(ctx) -> None:
    probe = ProbeResult.read(ctx.ws.probe_json)
    matcher = ctx.matcher
    if matcher is None:
        from vidcleaner.matching.compiler import build_matcher  # noqa: PLC0415

        matcher = build_matcher()
        ctx.matcher = matcher

    sidecars = find_sidecars(Path(probe.path))
    source, cues = _load_best_candidate(ctx, probe, sidecars)

    hits = find_hits(cues, matcher)

    # cues -> hits -> drift -> windows. §6 step 3 lists the windows first, but
    # the drift verdict chooses both the padding and the offset they are built
    # with, so it has to run before them. The check runs even when there are no
    # hits: zero hits means either "a clean episode" or "the wrong subtitle
    # file", and drift is the only thing that can tell those apart.
    drift_result = _measure_drift(ctx, cues, probe)
    drift_result.write(ctx.ws.drift_json)

    pad_s = WINDOW_PAD_S
    offset_s = drift_result.offset_s
    usable = True
    if drift_result.action == "discard":
        # Timing only. The cues stay -- they are the evidence for this verdict --
        # and so does `redactable`: redaction is text-local and correct whatever
        # the sync is.
        hits, offset_s, usable = [], 0.0, False
    elif drift_result.action == "unreliable":
        pad_s = ctx.settings.drift_window_pad_s

    windows = (
        cue_windows(cues, hits, pad_s=pad_s, duration=probe.duration or None, offset_s=offset_s)
        if usable
        else []
    )

    result = SubtitlesResult(
        source=source,
        cues=cues,
        hits=hits,
        windows=windows,
        offset_s=offset_s,
        reliable=drift_result.action != "unreliable",
        usable=usable,
        window_pad_s=pad_s,
        redactable=redactable_streams(probe),
        redactable_sidecars=[
            str(s)
            for s in redactable_sidecars(
                Path(probe.path), sidecars, preferred_language=ctx.settings.preferred_language
            )
        ],
        sidecars=[str(s) for s in sidecars],
    )
    ctx.log.info(
        "subtitles.done",
        kind=source.kind,
        reason=source.reason,
        language=source.language,
        cues=len(cues),
        hits=len(hits),
        windows=len(windows),
        window_seconds=round(sum(w.duration for w in windows), 1),
        redactable=len(result.redactable),
        drift=drift_result.action,
        drift_reason=drift_result.reason,
        offset_s=round(offset_s, 3),
        coverage=drift_result.coverage,
        pad_s=pad_s,
    )
    result.write(ctx.ws.subs_json)


def _measure_drift(ctx, cues, probe):
    """Run the drift check, or explain in the artifact why it did not run."""
    from vidcleaner.pipeline.artifacts import DriftResult  # noqa: PLC0415

    if not cues:
        return DriftResult(action="skipped", reason="no_cues")
    if not ctx.settings.drift_check:
        return DriftResult(action="skipped", reason="disabled")

    # Imported here, not at module scope: `drift` reaches a transcriber, and
    # `tests/unit/test_no_stt_import.py` asserts this module stays torch-free.
    from vidcleaner.pipeline import drift  # noqa: PLC0415

    return drift.measure_drift(ctx, cues, probe)


def load(ws: Workspace) -> SubtitlesResult:
    return SubtitlesResult.read(ws.subs_json)
