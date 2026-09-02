"""``vidcleaner clean`` / ``detect`` -- the M1 entry point (PLAN.md §11).

Kept out of ``cli.py`` so that module stays import-cheap: this one reaches into
the pipeline, and `tests/unit/test_no_stt_import.py` asserts that importing
``vidcleaner.cli`` pulls no torch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["add_arguments", "run_clean"]

_BAR_WIDTH = 28


def add_arguments(parser: argparse.ArgumentParser, *, with_output: bool) -> None:
    parser.add_argument("file", type=Path, help="the media file to process")
    if with_output:
        parser.add_argument(
            "--out",
            type=Path,
            help="write the cleaned MKV here instead of the work directory",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="stop after detection and print the word counts",
        )
    parser.add_argument(
        "--force", action="store_true", help="ignore stage markers and redo everything"
    )
    parser.add_argument(
        "--job-id", help="override the derived job id (which is what enables resume)"
    )
    parser.add_argument("--work-dir", type=Path, help="override the work directory")
    parser.add_argument(
        "--categories",
        help="comma-separated word categories to enable, overriding the profile",
    )
    parser.add_argument("--model", help="override the speech-to-text model")
    parser.add_argument(
        "--transcript",
        type=Path,
        help="replay this transcript.json instead of running speech-to-text",
    )
    parser.add_argument(
        "--detections",
        type=Path,
        help="use this detections.json and enter the pipeline at the render stage",
    )
    parser.add_argument(
        "--no-db", action="store_true", help="do not record the run in the database"
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="emit machine-readable JSON"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the progress bar and the report"
    )


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def _clock(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class _Progress:
    """A single-line progress bar, only when stderr is a terminal."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self._stage = ""

    def __call__(self, stage: str, fraction: float) -> None:
        if not self.enabled:
            return
        filled = int(_BAR_WIDTH * fraction)
        bar = "#" * filled + "." * (_BAR_WIDTH - filled)
        print(f"\r  {stage:<11} [{bar}] {fraction * 100:3.0f}%", end="", file=sys.stderr)
        self._stage = stage
        if fraction >= 1.0:
            print("", file=sys.stderr)

    def clear(self) -> None:
        if self.enabled and self._stage:
            print("", end="", file=sys.stderr)


def _report(result: Any, probe, subs, transcript, detections, render, verify) -> str:
    lines: list[str] = []
    codec = probe.clean_codec
    source = probe.source_audio
    bitrate = f" {codec.bit_rate // 1000}k" if codec.bit_rate else ""

    lines.append(f"Source     {probe.path}")
    lines.append(
        f"           {_human_size(probe.size)}, {_clock(probe.duration)}, {probe.container_format}"
    )
    lines.append(
        f"Audio      a:{source.typed_index} {source.codec_name} {source.channels}ch "
        f"{source.language or 'und'} ({probe.source_audio_reason})"
        f"  ->  {codec.encoder}{bitrate}   [{codec.reason}]"
    )
    if subs is not None:
        if subs.source.kind == "none":
            lines.append(f"Subtitles  none ({subs.source.reason})")
        else:
            lines.append(
                f"Subtitles  {subs.source.kind} {subs.source.language or 'und'} "
                f"({subs.source.reason})  {len(subs.cues)} cues, {len(subs.hits)} hits, "
                f"{len(subs.redactable)} redactable"
            )
    if transcript is not None and transcript.word_count:
        window_s = sum(w.duration for w in transcript.windows)
        lines.append(
            f"STT        {transcript.mode} {transcript.model}"
            f"{' + ' + transcript.align_model if transcript.align_model else ''}"
            f"  {len(transcript.windows)} windows / {_clock(window_s)}"
            f"  {transcript.word_count} words"
        )

    if detections is not None:
        stats = detections.stats
        lines.append("")
        lines.append(
            f"Detections {stats.get('detections', 0)}"
            f"  ({stats.get('muted', 0)} muted, {stats.get('suspicious', 0)} suspicious, "
            f"{stats.get('whitelisted', 0)} whitelisted)"
            f"  in {stats.get('ranges', 0)} ranges, {detections.total_muted_s:.1f}s muted"
        )
        if detections.counts:
            lines.append("")
            lines.append(f"  {'WORD':<20} {'CATEGORY':<10} {'COUNT':>5} {'MUTED':>5}  SOURCE")
            for row in detections.counts:
                sources = ", ".join(f"{k} {v}" for k, v in sorted(row.sources.items()))
                lines.append(
                    f"  {row.word_canonical:<20} {row.category:<10} "
                    f"{row.total:>5} {row.muted:>5}  {sources}"
                )
            lines.append(f"  {'-' * 56}")
            total = sum(r.total for r in detections.counts)
            muted = sum(r.muted for r in detections.counts)
            lines.append(f"  {'TOTAL':<20} {'':<10} {total:>5} {muted:>5}")

        suspicious = detections.suspicious
        if suspicious:
            lines.append("")
            lines.append(f"  {len(suspicious)} detection(s) need review:")
            for detection in suspicious:
                lines.append(
                    f"    ! {_clock(detection.mute_start_s)}.{int(detection.mute_start_s % 1 * 10)}"
                    f"  {detection.word_canonical:<14} {detection.source:<9}"
                    f" {detection.suspicious_reason or ''}"
                )

    if render is not None:
        lines.append("")
        redacted = sum(r.replacements for r in render.redacted)
        lines.append(
            f"Render     {_human_size(render.size)} in {_clock(render.elapsed_s)}"
            f"   {render.encoder}, {render.mute_range_count} ranges"
            + (f", {redacted} subtitle words masked" if redacted else "")
        )
        for warning in render.warnings:
            lines.append(f"           warning: {warning}")
    if verify is not None:
        status = "OK" if verify.ok else "FAILED"
        levels = ", ".join(f"{v:.1f}" for v in verify.measured_db)
        lines.append(
            f"Verify     {status}  ({len(verify.checks) - len(verify.failures)}"
            f"/{len(verify.checks)} checks" + (f"; measured {levels} dB" if levels else "") + ")"
        )
        for check in verify.warnings:
            lines.append(f"           warning: {check.name}: {check.detail}")
        for check in verify.failures:
            lines.append(f"           FAILED  {check.name}: {check.detail}")

    return "\n".join(lines)


def run_clean(args: argparse.Namespace, *, detect_only: bool = False) -> int:
    # Imported here so `vidcleaner --help` and `vidcleaner health` stay cheap.
    from vidcleaner.config import get_settings  # noqa: PLC0415
    from vidcleaner.db.session import session_scope  # noqa: PLC0415
    from vidcleaner.logging import configure_logging  # noqa: PLC0415
    from vidcleaner.matching.compiler import (  # noqa: PLC0415
        DEFAULT_CATEGORIES,
        ProfileSpec,
        build_matcher,
    )
    from vidcleaner.pipeline import (  # noqa: PLC0415
        detect as detect_stage,
    )
    from vidcleaner.pipeline import (
        probe as probe_stage,
    )
    from vidcleaner.pipeline import (
        render as render_stage,
    )
    from vidcleaner.pipeline import (
        stt as stt_stage,
    )
    from vidcleaner.pipeline import (
        subtitles as subs_stage,
    )
    from vidcleaner.pipeline import (
        verify as verify_stage,
    )
    from vidcleaner.pipeline.artifacts import DetectionResult, ProfileSnapshot  # noqa: PLC0415
    from vidcleaner.pipeline.stages import (  # noqa: PLC0415
        DRY_RUN_STAGES,
        M1_STAGES,
        StageError,
        build_context,
        build_spec,
        run_pipeline,
    )
    from vidcleaner.settings_store import AppSettings  # noqa: PLC0415

    # structlog's *default* factory writes to stdout, which would corrupt
    # --json. configure_logging targets stderr.
    configure_logging(
        "WARNING" if (args.as_json or args.quiet) else get_settings().log_level,
        role="cli",
    )

    source = args.file.expanduser()
    if not source.is_file():
        print(f"error: not a file: {source}", file=sys.stderr)
        return 66

    dry_run = detect_only or bool(getattr(args, "dry_run", False))
    deploy = get_settings()
    if args.work_dir:
        deploy = deploy.model_copy(update={"work_dir": args.work_dir.expanduser()})
    deploy.ensure_dirs()

    # Operational settings come from the database when there is one; a bare
    # checkout with no migrations still works on the defaults.
    settings = AppSettings()
    try:
        with session_scope() as session:
            from vidcleaner.settings_store import load_settings  # noqa: PLC0415

            settings = load_settings(session)
    except Exception:  # noqa: BLE001 - a missing database must not block the CLI
        pass

    if args.model:
        settings = settings.model_copy(
            update={"stt_windowed_model": args.model, "stt_full_model": args.model}
        )

    categories = DEFAULT_CATEGORIES
    if args.categories:
        categories = frozenset(c.strip() for c in args.categories.split(",") if c.strip())
    matcher = build_matcher(profile=ProfileSpec(categories=categories))

    profile = ProfileSnapshot(
        name=matcher.profile.name,
        categories=sorted(matcher.profile.categories),
        extra_canonicals=sorted(matcher.profile.extra_canonicals),
        pad_pre_ms=settings.pad_pre_ms,
        pad_post_ms=settings.pad_post_ms,
        merge_gap_ms=settings.merge_gap_ms,
        mute_censored_tokens=settings.mute_censored_tokens,
        profile_hash=matcher.profile_hash,
    )
    spec = build_spec(
        source,
        profile=profile,
        settings=settings,
        out=args.out.expanduser() if getattr(args, "out", None) else None,
        dry_run=dry_run,
        force=args.force,
        job_id=args.job_id,
    )

    progress = _Progress(enabled=not args.quiet and not args.as_json)
    transcriber = None
    if args.transcript:
        transcriber = stt_stage.ScriptedTranscriber(args.transcript.expanduser())

    ctx = build_context(
        spec, deploy=deploy, transcriber=transcriber, matcher=matcher, on_progress=progress
    )

    stages = list(DRY_RUN_STAGES if dry_run else M1_STAGES)
    if args.detections:
        # Enter at the render stage with a supplied detections file. Also the
        # mechanism behind "reprocess after a whitelist edit" in M4.
        DetectionResult.read(args.detections.expanduser()).write(ctx.ws.detections_json)
        # `probe` and `subtitles` are deliberately NOT marked: render needs
        # probe.json for the plan and subs.json for the redaction list, and a
        # fresh work dir has neither. Both are cheap and need no STT.
        for stage in ("extract", "transcribe", "detect"):
            ctx.ws.mark_done(stage)
        stages = [s for s in stages if s in {"probe", "subtitles", "render", "verify"}]

    state, error, failed_stage = "done", None, None
    try:
        run_pipeline(ctx, stages)
    except StageError as exc:
        state, error, failed_stage = "failed", exc.message, exc.stage
        progress.clear()
        print(f"error: {exc}", file=sys.stderr)

    probe = probe_stage.load(ctx.ws) if ctx.ws.probe_json.is_file() else None
    subs = subs_stage.load(ctx.ws) if ctx.ws.subs_json.is_file() else None
    transcript = stt_stage.load(ctx.ws)
    detections = detect_stage.load(ctx.ws) if ctx.ws.detections_json.is_file() else None
    render = render_stage.load(ctx.ws)
    verify = verify_stage.load(ctx.ws)

    if probe is not None and probe.already_clean and state == "done":
        state = "already_clean"

    if not args.no_db and probe is not None:
        try:
            from vidcleaner.pipeline.persist import persist_run  # noqa: PLC0415

            with session_scope() as session:
                persist_run(
                    session,
                    spec,
                    probe=probe,
                    detections=detections,
                    state=state,
                    stage=failed_stage,
                    error=error,
                    model_used=transcript.model if transcript else None,
                    subtitle_source=subs.source.reason if subs else None,
                    timings=None,
                    work_dir=ctx.ws.root,
                )
        except Exception as exc:  # noqa: BLE001 - never fail a good render on bookkeeping
            print(f"warning: could not record the run: {exc}", file=sys.stderr)

    if args.as_json:
        payload: dict[str, Any] = {
            "job_id": spec.job_id,
            "state": state,
            "dry_run": dry_run,
            "work_dir": str(ctx.ws.root),
            "profile_hash": spec.profile_hash,
            "error": error,
            "probe": probe.model_dump(mode="json") if probe else None,
            "detections": detections.model_dump(mode="json") if detections else None,
            "render": render.model_dump(mode="json") if render else None,
            "verify": verify.model_dump(mode="json") if verify else None,
        }
        print(json.dumps(payload, indent=2))
    elif not args.quiet and probe is not None:
        print(_report(None, probe, subs, transcript, detections, render, verify))
        print("")
        if state == "already_clean":
            print(f"Already clean for this profile ({spec.profile_hash}); nothing to do.")
        elif dry_run:
            print(f"Dry run -- nothing rendered.  Artifacts in {ctx.ws.root}")
        elif render is not None:
            print(f"Wrote      {render.out_path}")

    if state == "failed":
        return 1
    if verify is not None and not verify.ok:
        return 1
    return 0
