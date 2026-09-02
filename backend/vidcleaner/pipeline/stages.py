"""The stage contract, the stage registry, and the resumable driver.

CLAUDE.md requires every stage to be "a pure function of its on-disk inputs in
``/work/<job_id>/``". That needs one thing the convention does not name: the
job's own parameters must also be on disk, so ``job.json`` is written by the
driver before ``probe`` runs and is treated as artifact zero.

This module deliberately does **not** import ``pipeline.ffmpeg`` or
``pipeline.stt`` at module level. Stage modules are resolved through
``importlib`` on first use and the ffmpeg runner is injected, which is what keeps
``stages`` (and through it M4's API) importable on a checkout with no torch
installed. ``tests/unit/test_no_stt_import.py`` asserts that boundary.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from vidcleaner import __version__
from vidcleaner.config import Settings, get_settings
from vidcleaner.db.constants import JOB_STAGES
from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import (
    DetectionResult,
    JobSpec,
    JobTarget,
    ProbeResult,
    ProfileSnapshot,
    RenderResult,
    SubtitlesResult,
    Transcript,
    VerifyResult,
)
from vidcleaner.pipeline.workspace import Workspace
from vidcleaner.settings_store import SECRET_FIELDS, AppSettings

if TYPE_CHECKING:  # pragma: no cover
    from vidcleaner.matching.compiler import Matcher

__all__ = [
    "DRY_RUN_STAGES",
    "M1_STAGES",
    "M3_STAGES",
    "PipelineResult",
    "StageContext",
    "StageError",
    "StageOutcome",
    "StaleSourceError",
    "SwapBrokenError",
    "build_context",
    "build_spec",
    "deterministic_job_id",
    "get_stage",
    "run_pipeline",
    "run_stage",
]

log = get_logger(__name__)

#: The stages M1 implements. ``swap``/``refresh`` are M3 and ``snippets`` is M4.
M1_STAGES: Final[tuple[str, ...]] = (
    "probe",
    "extract",
    "subtitles",
    "transcribe",
    "detect",
    "render",
    "verify",
)
#: What a worker job runs: M1's stages plus the two that touch the library and the
#: outside world. ``snippets`` joins in M4.
M3_STAGES: Final[tuple[str, ...]] = (*M1_STAGES, "swap", "refresh")
#: PLAN.md §6: "dry_run jobs stop after detecting".
DRY_RUN_STAGES: Final[tuple[str, ...]] = M1_STAGES[: M1_STAGES.index("detect") + 1]

_STAGE_MODULES: Final[dict[str, str]] = {
    stage: f"vidcleaner.pipeline.{module}"
    for stage, module in (
        ("probe", "probe"),
        ("extract", "extract"),
        ("subtitles", "subtitles"),
        ("transcribe", "stt"),
        ("detect", "detect"),
        ("render", "render"),
        ("verify", "verify"),
        ("swap", "swap"),
        ("refresh", "refresh"),
    )
}


class StageError(RuntimeError):
    """A stage failed. No marker is written, so a retry resumes exactly there."""

    def __init__(self, stage: str, message: str, cause: BaseException | None = None) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message
        self.cause = cause


class StaleSourceError(StageError):
    """The source file changed or vanished under us.

    Distinct from a plain failure because §6's "path vanished" path is *recovery*, not
    defeat: the worker re-resolves the path through the arr and requeues once before
    giving up as ``stale``. Raised by ``probe`` and again by ``swap``, which re-checks
    immediately before the first rename -- minutes of rendering separate the two.
    """


class SwapBrokenError(StageError):
    """A library rename failed *and* so did its rollback.

    The one genuinely unrecoverable state in the pipeline. Never auto-retried: a
    human has to look at the two paths named in the message.
    """


class StageModule(Protocol):
    NAME: str

    def run(self, ctx: StageContext) -> None: ...

    def load(self, ws: Workspace) -> Any: ...


def _noop_progress(stage: str, fraction: float) -> None:
    return None


@dataclass
class StageContext:
    """Everything a stage needs. Constructed by :func:`build_context`."""

    spec: JobSpec
    ws: Workspace
    settings: AppSettings
    """From ``spec.settings``, not from the database: the job is self-describing."""
    deploy: Settings
    log: Any
    runner: Any = None
    """A ``pipeline.ffmpeg.FFmpegRunner``. Injected, so ``stages`` need not import it."""
    transcriber: Any = None
    """``None`` means "build one from settings" -- the DI seam for tests."""
    matcher: Matcher | None = None
    """Cached across subtitles/detect/render so the pattern compiles once."""
    integrations: Any = None
    """Live arr/Jellyfin clients for ``refresh``, injected by the worker.

    They cannot come from ``ctx.settings``: ``build_spec`` strips ``SECRET_FIELDS``
    from the snapshot precisely because ``/work`` ends up in bug reports, so
    ``job.json`` has no API keys and a stage cannot rebuild a client from it.
    ``None`` means "no integrations configured" and ``refresh`` skips."""
    on_progress: Callable[[str, float], None] = _noop_progress
    stage_registry: dict[str, str] = field(default_factory=lambda: dict(_STAGE_MODULES))

    def bind(self, stage: str) -> Any:
        return self.log.bind(stage=stage)

    def progress(self, stage: str, fraction: float) -> None:
        try:
            self.on_progress(stage, max(0.0, min(1.0, fraction)))
        except Exception:  # pragma: no cover - a reporter must never fail a job
            self.log.warning("progress.callback_failed", stage=stage, exc_info=True)


@dataclass(frozen=True, slots=True)
class StageOutcome:
    stage: str
    skipped: bool = False
    elapsed_s: float = 0.0
    result: Any = None


@dataclass
class PipelineResult:
    job_id: str
    outcomes: list[StageOutcome] = field(default_factory=list)
    probe: ProbeResult | None = None
    subtitles: SubtitlesResult | None = None
    transcript: Transcript | None = None
    detections: DetectionResult | None = None
    render: RenderResult | None = None
    verify: VerifyResult | None = None

    @property
    def timings(self) -> dict[str, float]:
        """Feeds ``jobs.timings_json`` in M3."""
        return {o.stage: round(o.elapsed_s, 3) for o in self.outcomes}

    @property
    def out_path(self) -> Path | None:
        return Path(self.render.out_path) if self.render else None

    @property
    def ok(self) -> bool:
        return self.verify.ok if self.verify is not None else True


def get_stage(name: str, registry: dict[str, str] | None = None) -> StageModule:
    """Import a stage module on demand, keeping heavy deps off the import path."""
    modules = registry or _STAGE_MODULES
    try:
        target = modules[name]
    except KeyError:
        raise ValueError(f"unknown stage {name!r}; expected one of {sorted(modules)}") from None
    return import_module(target)  # type: ignore[return-value]


def deterministic_job_id(
    source: Path, profile_hash: str, prefix: str = "cli", stt_mode: str = "windowed"
) -> str:
    """A stable id, so re-running ``clean`` resumes instead of redoing STT.

    Worth the two lines: it means the resume path is exercised every day rather
    than only by its unit test. M3's real jobs keep the ``jobs`` table's uuid4.

    ``stt_mode`` is part of the id because the profile hash deliberately excludes
    the STT model and mode (§14). Without it, ``--stt-mode full`` after a windowed
    run lands in the same work dir, finds ``transcribe.done``, and silently
    resumes onto the *windowed* transcript -- the expensive flag doing nothing at
    all. The default mode keeps its old id byte-for-byte, so existing work dirs
    still resume.
    """
    digest = hashlib.sha1(
        f"{source.resolve()}\0{profile_hash}".encode(), usedforsecurity=False
    ).hexdigest()
    suffix = "" if stt_mode == "windowed" else f"-{stt_mode}"
    return f"{prefix}-{digest[:10]}{suffix}"


def build_spec(
    source: Path,
    *,
    profile: ProfileSnapshot,
    settings: AppSettings,
    out: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
    stt_mode: str = "windowed",
    job_id: str | None = None,
    in_place: bool = False,
    trigger: str = "manual",
    target: JobTarget | None = None,
) -> JobSpec:
    """Build ``job.json``, with secrets stripped from the settings snapshot."""
    snapshot = {
        key: value for key, value in settings.model_dump().items() if key not in SECRET_FIELDS
    }
    return JobSpec(
        job_id=job_id or deterministic_job_id(source, profile.profile_hash, stt_mode=stt_mode),
        version=__version__,
        source_path=str(source),
        out_path=str(out) if out else None,
        dry_run=dry_run,
        force=force,
        stt_mode=stt_mode,  # type: ignore[arg-type]
        in_place=in_place,
        trigger=trigger,
        target=target,
        settings=snapshot,
        profile=profile,
        created_at=datetime.now(UTC),
    )


def new_job_id() -> str:
    return str(uuid.uuid4())


def build_context(
    spec: JobSpec,
    *,
    deploy: Settings | None = None,
    runner: Any = None,
    transcriber: Any = None,
    matcher: Matcher | None = None,
    on_progress: Callable[[str, float], None] | None = None,
) -> StageContext:
    """Materialise the work dir, write ``job.json``, and assemble the context."""
    deploy = deploy or get_settings()
    ws = Workspace.for_job(spec.job_id, deploy).ensure()
    spec.write(ws.job_spec)

    if runner is None:
        from vidcleaner.pipeline.ffmpeg import FFmpegRunner  # noqa: PLC0415

        runner = FFmpegRunner(log_path=ws.ffmpeg_log)

    return StageContext(
        spec=spec,
        ws=ws,
        settings=AppSettings.model_validate(spec.settings),
        deploy=deploy,
        log=log.bind(job_id=spec.job_id),
        runner=runner,
        transcriber=transcriber,
        matcher=matcher,
        on_progress=on_progress or _noop_progress,
    )


def run_stage(ctx: StageContext, stage: str, *, force: bool = False) -> StageOutcome:
    """Run one stage, or skip it when its marker is present and current."""
    module = get_stage(stage, ctx.stage_registry)
    bound = ctx.bind(stage)

    if not force and not ctx.spec.force and ctx.ws.is_done(stage):
        bound.info("stage.skipped", reason="marker")
        return StageOutcome(stage, skipped=True, result=module.load(ctx.ws))

    ctx.ws.clear_from(stage)
    bound.info("stage.start")
    started = time.monotonic()
    try:
        module.run(ctx)
    except StageError:
        raise
    except Exception as exc:
        bound.error("stage.failed", error=str(exc))
        raise StageError(stage, str(exc), exc) from exc

    elapsed = time.monotonic() - started
    ctx.ws.mark_done(stage, elapsed_s=elapsed)
    ctx.progress(stage, 1.0)
    bound.info("stage.done", elapsed_s=round(elapsed, 3))
    return StageOutcome(stage, elapsed_s=elapsed, result=module.load(ctx.ws))


def run_pipeline(ctx: StageContext, stages: Sequence[str] | None = None) -> PipelineResult:
    """Run stages in order, collecting artifacts.

    Failures are **not** caught: the CLI and (in M3) the worker runner own the
    retry and state-transition policy.
    """
    planned = (
        list(stages)
        if stages is not None
        else list(DRY_RUN_STAGES if ctx.spec.dry_run else M1_STAGES)
    )
    unknown = [s for s in planned if s not in JOB_STAGES]
    if unknown:
        raise ValueError(f"unknown stages: {unknown}")

    result = PipelineResult(job_id=ctx.spec.job_id)
    slots = {
        "probe": "probe",
        "subtitles": "subtitles",
        "transcribe": "transcript",
        "detect": "detections",
        "render": "render",
        "verify": "verify",
    }
    for stage in planned:
        outcome = run_stage(ctx, stage)
        result.outcomes.append(outcome)
        slot = slots.get(stage)
        if slot is not None and outcome.result is not None:
            setattr(result, slot, outcome.result)
    return result
