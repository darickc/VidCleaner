"""Stage 6: mux the clean track into a new MKV (PLAN.md §2, §6 step 6).

``build_render_command`` is pure and golden-tested, because every stream-index
bug in this milestone comes from confusing three different numbering schemes:

1. **``-map`` order determines output stream order.** ``-map 0:V`` goes first so
   video stays at absolute index 0; §2's "new first track" means the first
   *audio* track.
2. **``a:N`` / ``s:N`` in ``-c:``, ``-b:``, ``-disposition:`` and
   ``-metadata:s:`` are per-type ordinals over the *output* streams in ``-map``
   order** -- not input indices, and not absolute output indices.
3. **A ``-map [label]`` stream inherits no metadata or dispositions at all.**
   ``-map_metadata 0`` copies *global* tags only, so the clean track's title and
   language must be set explicitly. Mapped *input* streams do inherit both.

Output layout::

    abs 0        video          copy                 v:0
    abs 1        CLEAN audio    encoded              a:0   title=Clean, default
    abs 2..1+N   source audio   copy, all of them    a:1..a:N
    abs ...      subtitles      copy or redacted     s:0..s:M  (source order)
    abs ...      attachments    copy

Two §3 corrections are implemented here, both logged in §14: dispositions are
cleared **subtractively** (``-disposition:a:1 -default``), because the literal
``0`` zeroes the whole bitmask and destroys ``comment``/``original``/
``hearing_impaired`` on the user's original track; and ``mov_text`` is
transcoded to ``srt``, because it cannot be muxed into Matroska at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from vidcleaner import __version__
from vidcleaner.pipeline.artifacts import (
    BITMAP_SUBTITLE_CODECS,
    CodecPlan,
    DetectionResult,
    ProbeResult,
    RedactedSubtitle,
    RenderResult,
    SubtitleStreamInfo,
    TimeRange,
)
from vidcleaner.pipeline.ffmpeg import FfmpegCaps, get_caps
from vidcleaner.pipeline.graph import GraphSpec, build_mute_graph
from vidcleaner.pipeline.workspace import Workspace

NAME = "render"

__all__ = [
    "CLEAN_LABEL",
    "RenderPlan",
    "SubtitlePlan",
    "build_render_command",
    "load",
    "plan_render",
    "run",
    "subtitle_out_codec",
]

CLEAN_LABEL = "clean"
CLEAN_TITLE = "Clean"
ORIGINAL_TITLE = "Original"

#: `mov_text` and `text` cannot be muxed into Matroska; everything else copies.
_SUB_OUT_CODEC = {
    "mov_text": "srt",
    "text": "srt",
    "eia_608": "srt",
    "dvb_teletext": "srt",
}


def subtitle_out_codec(codec_name: str) -> str:
    return _SUB_OUT_CODEC.get(codec_name, "copy")


@dataclass(frozen=True, slots=True)
class SubtitlePlan:
    source_typed_index: int
    kind: Literal["text", "bitmap"]
    out_codec: str
    input_index: int = 0
    """0 = copied from the source; >0 = a redacted file supplied as an extra input."""
    language: str | None = None
    title: str | None = None
    dispositions: tuple[str, ...] = ()
    redacted_path: str | None = None
    replacements: int = 0
    tags_dropped: int = 0


@dataclass(frozen=True, slots=True)
class RenderPlan:
    source: Path
    output: Path
    graph: GraphSpec
    clean_source_ordinal: int
    clean_codec: CodecPlan
    clean_language: str | None
    source_audio: tuple[int, ...]
    """Typed indexes of every source audio stream, in source order."""
    default_audio_ordinals: tuple[int, ...]
    subtitles: tuple[SubtitlePlan, ...] = ()
    has_attachments: bool = False
    tags: dict[str, str] = field(default_factory=dict)
    duration: float = 0.0

    @property
    def original_output_ordinal(self) -> int:
        """Where the cleaned source track lands in the output.

        ``-map 0:a`` preserves source order, so it is ``1 + K`` for source
        ordinal ``K`` -- **not** a hardcoded ``a:1``, which mislabels any file
        whose cleaned track is not the first one.
        """
        return 1 + self.clean_source_ordinal

    @property
    def extra_inputs(self) -> tuple[str, ...]:
        seen: dict[int, str] = {}
        for plan in self.subtitles:
            if plan.input_index > 0 and plan.redacted_path:
                seen[plan.input_index] = plan.redacted_path
        return tuple(seen[i] for i in sorted(seen))


def _disposition_value(flags: tuple[str, ...]) -> str:
    """Rebuild a disposition for a stream that inherits nothing."""
    return "+".join(flags) if flags else "0"


def plan_render(
    probe: ProbeResult,
    detections: DetectionResult,
    *,
    output: Path,
    job_id: str,
    profile_hash: str,
    fade_ms: int = 0,
    redacted: dict[int, RedactedSubtitle] | None = None,
) -> RenderPlan:
    """Assemble everything :func:`build_render_command` needs. Pure."""
    source = probe.source_audio
    ranges = [TimeRange(start=r.start, end=r.end) for r in detections.mute_ranges]
    graph = build_mute_graph(
        ranges,
        in_label=f"0:a:{source.typed_index}",
        out_label=CLEAN_LABEL,
        fade_ms=fade_ms,
        duration=probe.duration or None,
    )

    redacted = redacted or {}
    next_input = 1
    plans: list[SubtitlePlan] = []
    for stream in probe.subtitles:
        replacement = redacted.get(stream.typed_index)
        input_index = 0
        out_codec = subtitle_out_codec(stream.codec_name)
        if replacement is not None and replacement.output_path:
            input_index = next_input
            next_input += 1
            out_codec = _redacted_codec(Path(replacement.output_path))
        plans.append(
            SubtitlePlan(
                source_typed_index=stream.typed_index,
                kind="bitmap" if stream.codec_name in BITMAP_SUBTITLE_CODECS else "text",
                out_codec=out_codec,
                input_index=input_index,
                language=stream.language,
                title=stream.title,
                dispositions=stream.dispositions,
                redacted_path=replacement.output_path if replacement else None,
                replacements=replacement.replacements if replacement else 0,
                tags_dropped=replacement.tags_dropped if replacement else 0,
            )
        )

    return RenderPlan(
        source=Path(probe.path),
        output=output,
        graph=graph,
        clean_source_ordinal=source.typed_index,
        clean_codec=probe.clean_codec,
        # Mirror the source exactly, absence included: asserting a language we
        # do not know is worse for Jellyfin and Infuse than leaving it unset.
        clean_language=source.language,
        source_audio=tuple(a.typed_index for a in probe.audio),
        default_audio_ordinals=tuple(a.typed_index for a in probe.audio if a.is_default),
        subtitles=tuple(plans),
        has_attachments=probe.attachment_count > 0,
        tags={
            "VIDCLEANER": "1",
            "VIDCLEANER_VERSION": __version__,
            "VIDCLEANER_PROFILE_HASH": profile_hash,
            "VIDCLEANER_JOB": job_id,
            "VIDCLEANER_SRC_FP": probe.fingerprint,
        },
        duration=probe.duration,
    )


def _redacted_codec(path: Path) -> str:
    return {".ass": "ass", ".ssa": "ass", ".vtt": "webvtt"}.get(path.suffix.lower(), "srt")


def build_render_command(plan: RenderPlan, caps: FfmpegCaps, graph_path: Path) -> list[str]:
    """The exact argv, minus the binary and the base flags. Pure."""
    args: list[str] = ["-i", str(plan.source)]
    for extra in plan.extra_inputs:
        args += ["-i", extra]

    args += [caps.filter_script_flag, str(graph_path)]

    # ---- mapping, in output order
    # Capital V excludes attached_pic: MP4 cover art copied as a video stream
    # into MKV is pointless at best and a mux failure at worst.
    args += ["-map", "0:V"]
    args += ["-map", f"[{plan.graph.out_label}]"]
    args += ["-map", "0:a"]
    for sub in plan.subtitles:
        if sub.input_index > 0:
            args += ["-map", f"{sub.input_index}:s:0"]
        else:
            args += ["-map", f"0:s:{sub.source_typed_index}"]
    if plan.has_attachments:
        args += ["-map", "0:t?"]

    # ---- codecs: blanket, then override the clean track only
    args += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]
    args += ["-c:a:0", plan.clean_codec.encoder]
    if plan.clean_codec.bit_rate:
        args += ["-b:a:0", str(plan.clean_codec.bit_rate)]
    if plan.clean_codec.channels:
        args += ["-ac:a:0", str(plan.clean_codec.channels)]
    if plan.clean_codec.sample_rate:
        args += ["-ar:a:0", str(plan.clean_codec.sample_rate)]
    args += list(plan.clean_codec.extra_args)

    for position, sub in enumerate(plan.subtitles):
        if sub.out_codec != "copy":
            args += [f"-c:s:{position}", sub.out_codec]

    # ---- dispositions
    args += ["-disposition:a:0", "default"]
    for ordinal in plan.default_audio_ordinals:
        # Subtractive: a literal `0` would zero the whole bitmask and destroy
        # `comment`, `original` and `hearing_impaired` on the user's track.
        args += [f"-disposition:a:{1 + ordinal}", "-default"]
    for position, sub in enumerate(plan.subtitles):
        if sub.input_index > 0:
            args += [f"-disposition:s:{position}", _disposition_value(sub.dispositions)]

    # ---- metadata
    args += ["-map_metadata", "0", "-map_chapters", "0"]
    args += ["-metadata:s:a:0", f"title={CLEAN_TITLE}"]
    if plan.clean_language:
        args += ["-metadata:s:a:0", f"language={plan.clean_language}"]
    args += [f"-metadata:s:a:{plan.original_output_ordinal}", f"title={ORIGINAL_TITLE}"]

    for position, sub in enumerate(plan.subtitles):
        if sub.input_index == 0:
            continue  # inherited from the source stream
        if sub.language:
            args += [f"-metadata:s:s:{position}", f"language={sub.language}"]
        if sub.title:
            args += [f"-metadata:s:s:{position}", f"title={sub.title}"]

    for key, value in plan.tags.items():
        args += ["-metadata", f"{key}={value}"]

    # Sparse subtitle streams otherwise provoke muxing-queue warnings.
    args += ["-max_interleave_delta", "0", "-f", "matroska", str(plan.output)]
    return args


# --------------------------------------------------------------------- stage


def _redact_subtitles(ctx, probe: ProbeResult, subs) -> dict[int, RedactedSubtitle]:
    """Redact the eligible streams into ``/work``. Never the library."""
    from vidcleaner.pipeline.subtitles import redact_file  # noqa: PLC0415

    if not ctx.settings.redact_subtitles or not subs.redactable:
        return {}

    out: dict[int, RedactedSubtitle] = {}
    for typed_index in subs.redactable:
        stream = next(s for s in probe.subtitles if s.typed_index == typed_index)
        source_path = _extracted_path(ctx, subs, probe, stream)
        if source_path is None:
            continue
        target = ctx.ws.redacted_dir / f"red_{typed_index}{source_path.suffix}"
        try:
            stats = redact_file(source_path, target, ctx.matcher)
        except Exception as exc:
            # A subtitle failure must never fail the render: drop the stream
            # from the plan and copy the original instead.
            ctx.log.warning("render.redaction_failed", stream=typed_index, error=str(exc))
            continue
        if stats.hits == 0:
            continue
        out[typed_index] = RedactedSubtitle(
            stream_typed_index=typed_index,
            output_path=str(target),
            replacements=stats.hits,
            tags_dropped=stats.tags_dropped,
        )
    return out


def _extracted_path(ctx, subs, probe: ProbeResult, stream: SubtitleStreamInfo) -> Path | None:
    """Reuse the subtitles stage's extraction, or pull the stream now."""
    if (
        subs.source.kind == "embedded"
        and subs.source.stream_typed_index == stream.typed_index
        and subs.source.path
    ):
        return Path(subs.source.path)

    from vidcleaner.pipeline.subtitles import _SUB_EXTRACT_FORMAT  # noqa: PLC0415

    codec, suffix = _SUB_EXTRACT_FORMAT.get(stream.codec_name, ("srt", ".srt"))
    target = ctx.ws.subs_dir / f"in_{stream.typed_index}{suffix}"
    if not target.is_file():
        try:
            ctx.runner.run(
                ["-i", probe.path, "-map", f"0:s:{stream.typed_index}", "-c:s", codec, str(target)],
                label=f"extract-sub-{stream.typed_index}",
                timeout=600,
            )
        except Exception as exc:
            ctx.log.warning("render.sub_extract_failed", stream=stream.typed_index, error=str(exc))
            return None
    return target


def run(ctx) -> None:
    from vidcleaner.pipeline.artifacts import SubtitlesResult  # noqa: PLC0415

    probe = ProbeResult.read(ctx.ws.probe_json)
    detections = DetectionResult.read(ctx.ws.detections_json)
    # Tolerate a missing subs.json: redaction is then simply skipped rather
    # than failing a render that is otherwise fine.
    subs = (
        SubtitlesResult.read(ctx.ws.subs_json) if ctx.ws.subs_json.is_file() else SubtitlesResult()
    )

    if ctx.matcher is None:
        from vidcleaner.matching.compiler import build_matcher  # noqa: PLC0415

        ctx.matcher = build_matcher()

    output = Path(ctx.spec.out_path) if ctx.spec.out_path else ctx.ws.out_mkv
    output.parent.mkdir(parents=True, exist_ok=True)

    redacted = _redact_subtitles(ctx, probe, subs)
    plan = plan_render(
        probe,
        detections,
        output=output,
        job_id=ctx.spec.job_id,
        profile_hash=ctx.spec.profile_hash,
        fade_ms=ctx.settings.fade_edges_ms,
        redacted=redacted,
    )

    caps = get_caps()
    args = build_render_command(plan, caps, ctx.ws.graph_txt)
    result = ctx.runner.run_filtered(
        input_args=args[: args.index(caps.filter_script_flag)],
        graph=plan.graph.text,
        graph_path=ctx.ws.graph_txt,
        output_args=args[args.index(caps.filter_script_flag) + 2 :],
        label="render",
        on_progress=lambda p: ctx.progress(NAME, p.fraction or 0.0),
        total_duration=plan.duration or None,
    )

    RenderResult(
        out_path=str(output),
        size=output.stat().st_size if output.is_file() else 0,
        elapsed_s=round(result.elapsed_s, 3),
        encoder=plan.clean_codec.encoder,
        bit_rate=plan.clean_codec.bit_rate,
        mute_range_count=plan.graph.n_ranges,
        filter_count=plan.graph.n_chunks + plan.graph.n_fades,
        redacted=list(redacted.values()),
        tags=plan.tags,
        warnings=list(plan.graph.warnings),
    ).write(ctx.ws.render_json)

    ctx.log.info(
        "render.done",
        out=str(output),
        size=output.stat().st_size if output.is_file() else 0,
        encoder=plan.clean_codec.encoder,
        ranges=plan.graph.n_ranges,
        chunks=plan.graph.n_chunks,
        redacted=len(redacted),
        elapsed_s=round(result.elapsed_s, 1),
    )


def load(ws: Workspace) -> RenderResult | None:
    return RenderResult.read(ws.render_json) if ws.render_json.is_file() else None
