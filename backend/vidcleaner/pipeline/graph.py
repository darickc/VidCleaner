"""Building the mute filter graph (PLAN.md §3).

Pure: no settings object, no filesystem, no ffmpeg. Padding and the 250 ms merge
happen upstream in ``detect``; this function only does *defensive* normalization
(drop empty ranges, clamp to the duration, sort, coalesce exact overlaps) so a
graph is never syntactically valid but semantically nonsense.

Three measured constraints shape the output, all logged in PLAN.md §14:

* ``av_expr_parse`` accepts at most **100** ``+`` terms in one expression (100
  parses, 101 fails), so ranges are chunked across chained ``volume`` filters.
  Chaining beats parenthesised grouping: no depth arithmetic, and each filter
  simply multiplies the gain by 0 or 1.
* ``asetnsamples`` needs **``:p=0``**; ``pad`` defaults to true and would
  zero-pad the final frame with up to 239 samples. The reframing itself is
  required because ``enable`` gates whole frames (32 ms for AC-3), which is
  audible; 240 samples is 5 ms at 48 kHz.
* Every ``afade`` must carry its own timeline ``enable``. An ungated
  ``afade=t=out`` holds its output at zero for the rest of the stream, so a
  chain of out/in pairs silences the whole file while passing every structural
  check.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from vidcleaner.pipeline.artifacts import TimeRange, merge_ranges

__all__ = [
    "FRAME_SAMPLES",
    "MAX_FADE_RANGES",
    "MAX_RANGES",
    "MAX_TERMS_PER_CHUNK",
    "GraphSpec",
    "build_mute_graph",
]

#: 5 ms at 48 kHz (5.44 ms at 44.1 kHz -- harmless; do not "fix" it).
FRAME_SAMPLES = 240
#: The hard ffmpeg limit is 100 terms; 90 leaves headroom for the if() guard.
MAX_TERMS_PER_CHUNK = 90
#: Above this, fades are dropped: each range costs two afade instances.
MAX_FADE_RANGES = 250
#: A film yields 60-120 merged ranges. Thousands means a detector bug, not a
#: dirty movie, so fail loudly rather than emit a pathological graph.
MAX_RANGES = 2000


@dataclass(frozen=True, slots=True)
class GraphSpec:
    text: str
    out_label: str
    n_ranges: int
    n_chunks: int
    n_fades: int
    warnings: tuple[str, ...] = ()


class GraphTooLarge(ValueError):
    """More mute ranges than any real file should produce."""


def _fmt(value: float) -> str:
    """Fixed-point milliseconds: locale-independent, never scientific notation."""
    return f"{value:.3f}"


def _floor_ms(value: float) -> float:
    return math.floor(value * 1000.0) / 1000.0


def _ceil_ms(value: float) -> float:
    return math.ceil(value * 1000.0) / 1000.0


def _normalize(ranges: Sequence[TimeRange], duration: float | None) -> list[TimeRange]:
    """Clamp, round outward, drop empties, sort and coalesce.

    Rounding outward -- start down, end up -- means millisecond quantization can
    only ever lengthen a mute, never leak a syllable.
    """
    cleaned: list[TimeRange] = []
    for candidate in ranges:
        start = max(0.0, candidate.start)
        end = candidate.end if duration is None else min(duration, candidate.end)
        if end <= start:
            continue
        cleaned.append(TimeRange(start=_floor_ms(start), end=_ceil_ms(end)))
    return merge_ranges(cleaned, 0.0)


def _chunk_expression(chunk: Sequence[TimeRange]) -> str:
    terms = "+".join(f"between(t,{_fmt(r.start)},{_fmt(r.end)})" for r in chunk)
    span_start = _fmt(chunk[0].start)
    span_end = _fmt(chunk[-1].end)
    # The outer if() short-circuits: outside the chunk's own span ffmpeg
    # evaluates one `between` instead of ninety. Emitted even for a single
    # chunk so there is exactly one output format to golden-test.
    return f"if(between(t,{span_start},{span_end}),{terms},0)"


def _fade_filters(ranges: Sequence[TimeRange], fade_s: float) -> list[str]:
    filters: list[str] = []
    for candidate in ranges:
        lead = min(fade_s, candidate.start)
        if lead > 0.0005:
            start = _fmt(candidate.start - lead)
            filters.append(
                f"afade=t=out:st={start}:d={_fmt(lead)}:curve=tri"
                f":enable='between(t,{start},{_fmt(candidate.start)})'"
            )
        end = _fmt(candidate.end)
        filters.append(
            f"afade=t=in:st={end}:d={_fmt(fade_s)}:curve=tri"
            f":enable='between(t,{end},{_fmt(candidate.end + fade_s)})'"
        )
    return filters


def build_mute_graph(
    ranges: Sequence[TimeRange],
    *,
    in_label: str = "0:a:0",
    out_label: str = "clean",
    frame_samples: int = FRAME_SAMPLES,
    fade_ms: int = 0,
    duration: float | None = None,
    max_terms_per_chunk: int = MAX_TERMS_PER_CHUNK,
) -> GraphSpec:
    """Turn mute ranges into a filter graph. Pure."""
    normalized = _normalize(ranges, duration)
    if len(normalized) > MAX_RANGES:
        raise GraphTooLarge(
            f"{len(normalized)} mute ranges exceeds the {MAX_RANGES} guard; "
            "this indicates a detector fault rather than a very profane film"
        )

    warnings: list[str] = []

    if not normalized:
        return GraphSpec(f"[{in_label}]anull[{out_label}]", out_label, 0, 0, 0, ())

    filters = [f"asetnsamples=n={frame_samples}:p=0"]

    n_fades = 0
    if fade_ms > 0:
        if len(normalized) > MAX_FADE_RANGES:
            warnings.append(f"fades_dropped: {len(normalized)} ranges > {MAX_FADE_RANGES}")
        else:
            # Before the volume chain: multiplication commutes, and this keeps
            # the fade_ms=0 vs fade_ms>0 diff a pure prefix insertion.
            fades = _fade_filters(normalized, fade_ms / 1000.0)
            filters.extend(fades)
            n_fades = len(fades)

    chunks = [
        normalized[i : i + max_terms_per_chunk]
        for i in range(0, len(normalized), max_terms_per_chunk)
    ]
    filters.extend(f"volume=0:enable='{_chunk_expression(chunk)}'" for chunk in chunks)

    text = f"[{in_label}]" + ",".join(filters) + f"[{out_label}]"
    return GraphSpec(text, out_label, len(normalized), len(chunks), n_fades, tuple(warnings))
