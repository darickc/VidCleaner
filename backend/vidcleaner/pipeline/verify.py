"""Stage 7: prove the output is safe before anything touches the library (§6 step 7).

Split into a pure half (structural comparison of two ``ProbeResult``s) and an
ffmpeg-backed half (decode and level measurement), so most of it is testable
without media.

Every check always runs and always yields a :class:`Check`, so the result is a
green/amber/red list rather than a single error string. Severity means: **fatal**
= the output is worse than the original for playback, or data was lost; **warn**
= metadata drift.

The single most important check is ``control_window_audible``. Everything else
would pass on a file whose audio had been silenced end to end -- which is
exactly what an ungated ``afade`` chain, a mis-signed time offset, or an
``enable`` expression that evaluates true everywhere all produce.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from vidcleaner.pipeline.artifacts import (
    Check,
    ProbeResult,
    TimeRange,
    VerifyResult,
)
from vidcleaner.pipeline.ffmpeg import INAUDIBLE_DB
from vidcleaner.pipeline.render import CLEAN_TITLE, ORIGINAL_TITLE, RenderPlan
from vidcleaner.pipeline.workspace import Workspace

NAME = "verify"

__all__ = [
    "AUDIBLE_DB",
    "DURATION_TOLERANCE_S",
    "SIZE_FLOOR",
    "load",
    "pick_control_window",
    "pick_probe_windows",
    "run",
    "structural_checks",
    "verify_render",
]

DURATION_TOLERANCE_S = 0.5
SIZE_FLOOR = 0.9
#: A 1 kHz tone or ordinary dialogue sits far above this.
AUDIBLE_DB = -50.0
#: Measured: a nominal [3.000, 3.200] mute lands at [3.0056, 3.2101] after a
#: real AC-3 round trip, because the MDCT window smears the edges by about one
#: frame. Asserting at the nominal boundary would flake.
WINDOW_INSET_S = 0.04
MIN_PROBE_WINDOW_S = 0.25
MAX_PROBE_WINDOWS = 3
CONTROL_WINDOW_S = 1.0
CONTROL_GUARD_S = 0.5

#: Text subtitle transcodes we deliberately perform (`mov_text` cannot be muxed
#: into Matroska at all), so they are a warning rather than a failure.
ALLOWED_SUB_TRANSCODES = {("mov_text", "subrip"), ("text", "subrip")}


def _check(name: str, ok: bool, detail: str = "", severity: str = "fatal", **kw) -> Check:
    return Check(name=name, ok=ok, detail=detail, severity=severity, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------ window choice


def pick_probe_windows(
    ranges: Sequence[TimeRange],
    *,
    max_windows: int = MAX_PROBE_WINDOWS,
    min_duration: float = MIN_PROBE_WINDOW_S,
    inset: float = WINDOW_INSET_S,
) -> list[TimeRange]:
    """The longest few mute ranges, inset so codec smear cannot cause a flake."""
    usable = sorted(
        (r for r in ranges if r.duration >= min_duration),
        key=lambda r: r.duration,
        reverse=True,
    )
    out: list[TimeRange] = []
    for candidate in usable[:max_windows]:
        start, end = candidate.start + inset, candidate.end - inset
        if end > start:
            out.append(TimeRange(start=start, end=end))
    return sorted(out, key=lambda r: r.start)


def pick_control_window(
    ranges: Sequence[TimeRange],
    duration: float,
    *,
    length: float = CONTROL_WINDOW_S,
    guard: float = CONTROL_GUARD_S,
) -> TimeRange | None:
    """A stretch of audio that should be untouched, preferring the midpoint."""
    if duration <= length:
        return None
    blocked = [TimeRange(start=max(0.0, r.start - guard), end=r.end + guard) for r in ranges]

    def free(start: float) -> bool:
        window = TimeRange(start=start, end=start + length)
        return not any(window.overlaps(b) for b in blocked)

    midpoint = max(0.0, duration / 2.0 - length / 2.0)
    step = max(length, 1.0)
    candidates = [midpoint]
    offset = step
    while offset < duration:
        candidates += [midpoint + offset, midpoint - offset]
        offset += step

    for start in candidates:
        if 0.0 <= start <= duration - length and free(start):
            return TimeRange(start=start, end=start + length)
    return None


# ------------------------------------------------------------------ structural


def structural_checks(source: ProbeResult, output: ProbeResult, plan: RenderPlan) -> list[Check]:
    """Compare two probes. Pure, so it runs against synthetic inputs in tests."""
    checks: list[Check] = []

    expected_audio = len(source.audio) + 1
    checks.append(
        _check(
            "audio_stream_count",
            len(output.audio) == expected_audio,
            f"{len(output.audio)} audio streams",
            expected=str(expected_audio),
            actual=str(len(output.audio)),
        )
    )

    src_video = [
        (v.codec_name, v.width, v.height, v.pix_fmt) for v in source.video if not v.is_attached_pic
    ]
    out_video = [
        (v.codec_name, v.width, v.height, v.pix_fmt) for v in output.video if not v.is_attached_pic
    ]
    checks.append(
        _check(
            "video_streams_identical",
            src_video == out_video,
            "video must be stream-copied unchanged",
            expected=str(src_video),
            actual=str(out_video),
        )
    )

    clean = output.audio[0] if output.audio else None
    checks.append(_check("clean_track_is_audio_zero", clean is not None, "no audio in the output"))

    if clean is not None:
        checks.append(
            _check(
                "clean_track_title",
                clean.title == CLEAN_TITLE,
                f"a:0 title is {clean.title!r}",
                expected=CLEAN_TITLE,
                actual=str(clean.title),
            )
        )
        checks.append(
            _check("clean_track_default", clean.is_default, "a:0 must carry the default flag")
        )
        checks.append(
            _check(
                "clean_track_codec",
                clean.codec_name == plan.clean_codec.encoder,
                f"a:0 is {clean.codec_name}",
                severity="warn",
                expected=plan.clean_codec.encoder,
                actual=clean.codec_name,
            )
        )
        expected_channels = source.source_audio.channels
        checks.append(
            _check(
                "clean_track_channels",
                clean.channels == expected_channels,
                f"a:0 has {clean.channels} channels",
                expected=str(expected_channels),
                actual=str(clean.channels),
            )
        )
        if plan.clean_language:
            checks.append(
                _check(
                    "clean_track_language",
                    clean.language == plan.clean_language,
                    f"a:0 language is {clean.language!r}",
                    expected=plan.clean_language,
                    actual=str(clean.language),
                )
            )
        else:
            checks.append(
                _check(
                    "clean_track_language",
                    clean.language is None,
                    "source audio had no language tag, so the clean track has none",
                    severity="warn",
                )
            )

    defaults = [a for a in output.audio if a.is_default]
    checks.append(
        _check(
            "single_default_audio",
            len(defaults) == 1,
            f"{len(defaults)} audio streams carry the default flag",
        )
    )

    ordinal = plan.original_output_ordinal
    if len(output.audio) > ordinal:
        original = output.audio[ordinal]
        checks.append(
            _check(
                "original_track_title",
                original.title == ORIGINAL_TITLE,
                f"a:{ordinal} title is {original.title!r}",
                severity="warn",
                expected=ORIGINAL_TITLE,
                actual=str(original.title),
            )
        )
        src_flags = set(source.source_audio.dispositions) - {"default"}
        out_flags = set(original.dispositions) - {"default"}
        checks.append(
            _check(
                "original_dispositions_preserved",
                src_flags <= out_flags,
                "clearing `default` must not wipe the other disposition flags",
                severity="warn",
                expected=str(sorted(src_flags)),
                actual=str(sorted(out_flags)),
            )
        )

    checks.append(
        _check(
            "subtitle_stream_count",
            len(output.subtitles) == len(source.subtitles),
            f"{len(output.subtitles)} of {len(source.subtitles)} subtitle streams",
        )
    )

    codec_problems: list[str] = []
    bitmap_problems: list[str] = []
    for src, out in zip(source.subtitles, output.subtitles, strict=False):
        if src.codec_name == out.codec_name:
            continue
        pair = (src.codec_name, out.codec_name)
        if src.is_bitmap:
            bitmap_problems.append(f"s:{src.typed_index} {src.codec_name}->{out.codec_name}")
        elif pair not in ALLOWED_SUB_TRANSCODES:
            codec_problems.append(f"s:{src.typed_index} {src.codec_name}->{out.codec_name}")
    checks.append(
        _check(
            "bitmap_subtitles_untouched",
            not bitmap_problems,
            ", ".join(bitmap_problems) or "bitmap subtitles copied unchanged",
        )
    )
    checks.append(
        _check(
            "subtitle_codecs",
            not codec_problems,
            ", ".join(codec_problems) or "subtitle codecs as planned",
            severity="warn",
        )
    )

    checks.append(
        _check(
            "chapters_preserved",
            output.chapter_count >= source.chapter_count
            or (source.chapter_count > 0 and output.chapter_count > 0),
            f"{output.chapter_count} of {source.chapter_count} chapters",
            severity="fatal" if source.chapter_count and not output.chapter_count else "warn",
        )
    )
    checks.append(
        _check(
            "attachments_preserved",
            output.attachment_count >= source.attachment_count,
            f"{output.attachment_count} of {source.attachment_count} attachments",
            severity="warn",
        )
    )

    drift = abs(output.duration - source.duration)
    checks.append(
        _check(
            "duration_within_tolerance",
            drift <= DURATION_TOLERANCE_S,
            f"duration differs by {drift:.3f}s",
            expected=f"<= {DURATION_TOLERANCE_S}s",
            actual=f"{drift:.3f}s",
        )
    )
    checks.append(
        _check(
            "output_size_floor",
            output.size >= source.size * SIZE_FLOOR,
            f"output is {output.size / max(1, source.size):.2f}x the source",
            expected=f">= {SIZE_FLOOR:g}x",
        )
    )

    missing = [k for k in plan.tags if k not in output.tags]
    checks.append(
        _check(
            "idempotency_tags",
            not missing,
            f"missing tags: {missing}" if missing else "all VIDCLEANER tags present",
        )
    )
    checks.append(
        _check(
            "source_fingerprint_tag",
            output.tags.get("VIDCLEANER_SRC_FP") == plan.tags.get("VIDCLEANER_SRC_FP"),
            "VIDCLEANER_SRC_FP must record the source we actually read",
        )
    )
    return checks


# ---------------------------------------------------------------- acoustic


def _acoustic_checks(runner, output: Path, plan: RenderPlan, ranges: Sequence[TimeRange]):
    checks: list[Check] = []
    measured: list[float] = []

    stderr = runner.decode_check(output, stream="0:a:0")
    checks.append(
        _check("clean_track_decodes", not stderr, stderr[-300:] or "decodes without error")
    )

    for index, window in enumerate(pick_probe_windows(ranges)):
        stats = runner.measure_volume(output, stream="0:a:0", window=window)
        if stats is None:
            checks.append(
                _check(f"mute_window_silent[{index}]", False, f"no measurement for {window}")
            )
            continue
        measured.append(stats.max_db)
        checks.append(
            _check(
                f"mute_window_silent[{index}]",
                stats.max_db <= INAUDIBLE_DB,
                f"{window.start:.2f}-{window.end:.2f}s peaks at {stats.max_db:.1f} dB",
                expected=f"<= {INAUDIBLE_DB:g} dB",
                actual=f"{stats.max_db:.1f} dB",
            )
        )

    control = pick_control_window(ranges, plan.duration)
    if control is None:
        checks.append(
            _check(
                "control_window_audible",
                True,
                "no window clear of the mutes was available",
                severity="warn",
            )
        )
    else:
        stats = runner.measure_volume(output, stream="0:a:0", window=control)
        if stats is None:
            checks.append(_check("control_window_audible", False, "no measurement"))
        else:
            measured.append(stats.max_db)
            checks.append(
                _check(
                    "control_window_audible",
                    stats.max_db >= AUDIBLE_DB,
                    f"{control.start:.2f}-{control.end:.2f}s peaks at {stats.max_db:.1f} dB",
                    expected=f">= {AUDIBLE_DB:g} dB",
                    actual=f"{stats.max_db:.1f} dB",
                )
            )
    return checks, measured


def verify_render(
    runner,
    *,
    source: ProbeResult,
    plan: RenderPlan,
    mute_ranges: Sequence[TimeRange],
    output_probe: ProbeResult,
    output_path: Path,
) -> VerifyResult:
    checks = structural_checks(source, output_probe, plan)

    stat_ok = True
    try:
        stat = Path(source.path).stat()
        stat_ok = stat.st_size == source.size and (
            source.inode is None or stat.st_ino == source.inode
        )
    except OSError:
        stat_ok = False
    checks.append(
        _check("source_unchanged", stat_ok, "the source file must be untouched by the render")
    )

    acoustic, measured = _acoustic_checks(runner, output_path, plan, mute_ranges)
    checks += acoustic

    result = VerifyResult(
        ok=not any(c.severity == "fatal" and not c.ok for c in checks),
        checks=checks,
        measured_db=measured,
    )
    return result


# --------------------------------------------------------------------- stage


def run(ctx) -> None:
    from vidcleaner.pipeline.artifacts import DetectionResult, RenderResult, SubtitlesResult
    from vidcleaner.pipeline.probe import parse_probe
    from vidcleaner.pipeline.render import plan_render

    probe = ProbeResult.read(ctx.ws.probe_json)
    detections = DetectionResult.read(ctx.ws.detections_json)
    render = RenderResult.read(ctx.ws.render_json)
    SubtitlesResult.read(ctx.ws.subs_json)

    output_path = Path(render.out_path)
    plan = plan_render(
        probe,
        detections,
        output=output_path,
        job_id=ctx.spec.job_id,
        profile_hash=ctx.spec.profile_hash,
        fade_ms=ctx.settings.fade_edges_ms,
    )

    out_stat = output_path.stat()
    output_probe = parse_probe(
        ctx.runner.probe(output_path),
        path=output_path,
        size=out_stat.st_size,
        mtime=out_stat.st_mtime,
        preferred_language=ctx.settings.preferred_language,
        lossless=ctx.settings.clean_track_lossless,
    )

    result = verify_render(
        ctx.runner,
        source=probe,
        plan=plan,
        mute_ranges=detections.mute_ranges,
        output_probe=output_probe,
        output_path=output_path,
    )
    result.write(ctx.ws.verify_json)

    for check in result.checks:
        if not check.ok:
            ctx.log.warning(
                "verify.check_failed",
                check=check.name,
                severity=check.severity,
                detail=check.detail,
            )
    ctx.log.info(
        "verify.done",
        ok=result.ok,
        checks=len(result.checks),
        failures=len(result.failures),
        warnings=len(result.warnings),
        measured_db=[round(v, 1) for v in result.measured_db],
    )
    if not result.ok:
        names = ", ".join(c.name for c in result.failures)
        raise RuntimeError(f"verification failed: {names}")


def load(ws: Workspace) -> VerifyResult | None:
    return VerifyResult.read(ws.verify_json) if ws.verify_json.is_file() else None
