"""The worker loop: claim a job, drive the stages, record what happened.

A separate process from the api because CTranslate2 and numpy hold the GIL and pin
threads, which would starve the event loop (PLAN.md §4).

The loop does not call ``run_pipeline``: it walks the stages itself, so it can write
``jobs.state`` per stage, check for a cancellation between them, and re-weight the
progress bar when a job is promoted to a full-file pass. ``run_pipeline`` stays the
CLI's path and the one used in tests.

Division of periodic work (see the Decision Log): this process owns queue and disk
maintenance -- stale recovery, `/work` collection, the idle-gated audit pass -- while
the api owns the hourly arr sync, because a timer here fires however late the current
ffmpeg or STT stage happens to be.
"""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from vidcleaner.config import Settings, get_settings
from vidcleaner.db.constants import STAGE_TO_STATE
from vidcleaner.db.models import Job
from vidcleaner.db.session import get_engine, session_scope, utcnow
from vidcleaner.logging import get_logger
from vidcleaner.pipeline.stages import (
    DRY_RUN_STAGES,
    M4_STAGES,
    StageError,
    build_context,
)
from vidcleaner.worker import claim as queue
from vidcleaner.worker.gc import prune_work_dir
from vidcleaner.worker.heartbeat import JobMonitor
from vidcleaner.worker.joblog import Timeline
from vidcleaner.worker.policy import classify
from vidcleaner.worker.progress import STAGE_WEIGHTS_FULL, ProgressTracker
from vidcleaner.worker.scheduler import Scheduler
from vidcleaner.worker.spec import plan_job

log = get_logger(__name__)

POLL_INTERVAL_S = 5.0


def worker_id() -> str:
    """Identifies this process in ``jobs.claimed_by``."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    job_id: str
    state: str
    stage: str | None = None
    error: str | None = None


class Worker:
    def __init__(
        self,
        settings: Settings | None = None,
        poll_interval: float = POLL_INTERVAL_S,
        *,
        transcriber: Any = None,
    ):
        self.settings = settings or get_settings()
        self.poll_interval = poll_interval
        self.transcriber = transcriber
        """``None`` means "build one from settings". The same DI seam
        ``build_context`` already exposes, and what lets the ffmpeg integration tier
        drive a real job without downloading a 2 GB model."""
        self.id = worker_id()
        self._stop = threading.Event()
        self.scheduler = Scheduler(self.settings)
        _register_swap_reconciler()

    def request_stop(self) -> None:
        """Signal-safe: wakes the loop out of its sleep so shutdown is immediate."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def check_database(self) -> None:
        with get_engine(self.settings).connect() as connection:
            connection.execute(text("SELECT 1"))

    # ------------------------------------------------------------------ loop

    def poll_once(self) -> bool:
        """Claim and run one job. Returns True if work was done."""
        claimed = queue.claim_next(worker_id=self.id, settings=self.settings)
        if claimed is None:
            return False
        try:
            outcome = self.run_job(claimed)
            log.info("job.finished", job_id=outcome.job_id, state=outcome.state)
        except Exception:
            # A crash *outside* the stage machine (a bug here, or the database going
            # away) must not leave the job claimed forever; stale recovery would
            # eventually free it, but two minutes of a dead worker is avoidable.
            log.exception("job.runner_failed", job_id=claimed.job_id)
            with session_scope() as session:
                queue.release(session, job_id=claimed.job_id, error="worker error")
        return True

    def run(self) -> None:
        self.check_database()
        log.info(
            "worker.startup",
            worker_id=self.id,
            work_dir=str(self.settings.work_dir),
            poll_interval_s=self.poll_interval,
        )
        # A worker that died mid-job left its claim behind; take it back before
        # looking for new work.
        with session_scope() as session:
            queue.recover_stale(session)

        while not self.stopping:
            try:
                did_work = self.poll_once()
            except Exception:
                log.exception("worker.poll_failed")
                did_work = False
            if not did_work:
                # Periodic work runs only when the queue is idle, which is exactly
                # what §6 requires of the audit pass and costs the rest nothing.
                try:
                    self.scheduler.tick()
                except Exception:
                    log.exception("worker.scheduler_failed")
                self._stop.wait(self.poll_interval)
        log.info("worker.shutdown", worker_id=self.id)

    # ------------------------------------------------------------------- job

    def run_job(self, claimed: queue.Claim) -> JobOutcome:
        timeline = Timeline(claimed.job_id)
        timeline.info("claimed", worker=self.id, trigger=claimed.trigger, attempt=claimed.attempts)

        with session_scope() as session:
            job = session.get(Job, claimed.job_id)
            if job is None:  # pragma: no cover - the row was deleted under us
                return JobOutcome(claimed.job_id, "failed", error="the job row is gone")
            _restore_before_reclean(session, job, timeline)
            plan = plan_job(session, job, deploy=self.settings)
            item_id = plan.item.id
            arr_paths = (plan.title.arr_path,) if plan.title and plan.title.arr_path else ()
            integrations = _integrations_for(session)

        if plan.invalidated:
            timeline.warning("work dir discarded", reason=plan.invalidated)

        stages = list(DRY_RUN_STAGES if plan.spec.dry_run else M4_STAGES)
        tracker = ProgressTracker(stages, completed=plan.completed)
        if plan.settings.render_parallel > 1:
            # §4 wants render of job N to overlap STT of N+1, which the stage driver
            # cannot express: run_pipeline is strictly sequential over one work dir.
            timeline.warning("render_parallel has no effect", value=plan.settings.render_parallel)

        monitor = JobMonitor(
            job_id=claimed.job_id,
            worker_id=self.id,
            tracker=tracker,
            settings=self.settings,
        )
        ctx = build_context(
            plan.spec,
            deploy=self.settings,
            matcher=plan.matcher,
            transcriber=self.transcriber,
            on_progress=monitor.report,
        )
        ctx.integrations = integrations
        ctx.arr_paths = arr_paths  # type: ignore[attr-defined]

        # Deliberately `None` until `_drive` returns. An earlier version seeded it
        # with "done" and recorded it in `finally`, so anything that escaped the
        # stage machine -- a `BaseException` such as `KeyboardInterrupt`, which
        # `poll_once` does not catch -- marked an unfinished job **done** and its
        # item **clean**, with no swap. Now an escape records nothing and leaves the
        # claim, which is exactly what a killed process leaves for stale recovery.
        outcome: JobOutcome | None = None
        try:
            with monitor:
                outcome = self._drive(ctx, claimed, plan, stages, monitor, timeline)
        finally:
            if integrations is not None:
                integrations.close()
            if outcome is not None:
                self._record(ctx, claimed, plan, outcome, item_id, timeline)
            else:
                timeline.error("the worker stopped without finishing this job")
            timeline.flush()
        return outcome

    def _drive(self, ctx, claimed, plan, stages, monitor, timeline) -> JobOutcome:
        from vidcleaner.pipeline import probe as probe_stage  # noqa: PLC0415
        from vidcleaner.pipeline import stt as stt_stage  # noqa: PLC0415
        from vidcleaner.pipeline import subtitles as subs_stage  # noqa: PLC0415
        from vidcleaner.pipeline.stages import run_stage  # noqa: PLC0415

        # §6.1: a webhook can fire while the import is still copying or hardlinking.
        # Only for webhook jobs -- everything else would pay 10 s for nothing.
        if claimed.trigger == "webhook" and not ctx.ws.is_done("probe"):
            monitor.set_stage("probe", "probing")
            timeline.info("waiting for the source to settle")
            probe_stage.wait_for_stable(Path(plan.spec.source_path))

        for stage in stages:
            if monitor.cancelled:
                timeline.warning("stopped before " + stage, reason=monitor.reason)
                return JobOutcome(claimed.job_id, "cancelled", stage=stage)

            monitor.set_stage(stage, STAGE_TO_STATE[stage])
            try:
                result = run_stage(ctx, stage)
            except StageError as exc:
                return self._on_stage_error(claimed, stage, exc, timeline)

            timeline.info(
                f"{stage} {'skipped' if result.skipped else 'done'}",
                elapsed_s=round(result.elapsed_s, 2) or None,
            )

            if stage == "probe":
                stop = self._after_probe(ctx, claimed, plan, timeline)
                if stop is not None:
                    return stop
            elif stage == "subtitles":
                self._maybe_full_mode(ctx, plan, subs_stage, stt_stage, monitor, timeline)
            elif stage == "transcribe":
                _log_transcript(timeline, stt_stage.load(ctx.ws))
            elif stage == "detect":
                _log_detections(timeline, result.result)
            elif stage == "swap":
                _log_swap(timeline, result.result)
            elif stage == "refresh":
                _log_refresh(timeline, result.result)

        return JobOutcome(claimed.job_id, "done")

    def _after_probe(self, ctx, claimed, plan, timeline) -> JobOutcome | None:
        from vidcleaner.pipeline import probe as probe_stage  # noqa: PLC0415

        probe = probe_stage.load(ctx.ws)
        if probe.already_clean and not plan.spec.force:
            # §4's idempotency loop. `parse_probe` has always computed this; nothing
            # acted on it outside the CLI's post-hoc fixup.
            timeline.info("already clean for this profile", hash=plan.spec.profile_hash)
            return JobOutcome(claimed.job_id, "already_clean", stage="probe")

        # §6.1 calls this a guard; it only logged a warning before.
        problems = probe_stage.check_free_space(
            probe, self.settings.work_dir, self.settings.backups_dir if plan.spec.in_place else None
        )
        if problems and not plan.spec.dry_run:
            timeline.error("not enough free space", detail="; ".join(problems))
            return JobOutcome(claimed.job_id, "failed", stage="probe", error="; ".join(problems))
        return None

    def _maybe_full_mode(self, ctx, plan, subs_stage, stt_stage, monitor, timeline) -> None:
        """Re-weight the bar if this job is heading for a full-file pass.

        The promotion is decided inside the transcribe stage, so the runner cannot
        know at claim time; but by now `subs.json` holds everything that decides it.
        """
        from vidcleaner.pipeline import probe as probe_stage  # noqa: PLC0415

        subs = subs_stage.load(ctx.ws)
        probe = probe_stage.load(ctx.ws)
        mode, reason = stt_stage.resolve_mode(
            plan.spec.stt_mode, subs, duration_s=probe.duration, settings=plan.settings
        )
        timeline.info(
            "subtitles",
            source=subs.source.reason,
            cues=len(subs.cues),
            hits=len(subs.hits),
            windows=len(subs.windows),
            mode=f"{mode} ({reason})" if reason else mode,
        )
        if mode in ("full", "audit"):
            monitor.tracker.reweight(STAGE_WEIGHTS_FULL)

    def _on_stage_error(self, claimed, stage, exc: StageError, timeline) -> JobOutcome:
        verdict = classify(stage, exc.cause or exc, attempts=claimed.attempts)
        timeline.error(f"{stage} failed", error=exc.message, verdict=verdict.verdict)
        if verdict.verdict == "best_effort":
            # The swap already committed: the library file is correct and a retry
            # would re-run the one non-idempotent stage. §6 step 9 says "warn".
            timeline.warning("continuing", detail=verdict.detail)
            return JobOutcome(claimed.job_id, "done", stage=stage, error=exc.message)
        if verdict.retry_in_s is not None:
            with session_scope() as session:
                queue.release(
                    session,
                    job_id=claimed.job_id,
                    retry_at=utcnow() + timedelta(seconds=verdict.retry_in_s),
                    error=exc.message,
                )
            timeline.info("retry scheduled", seconds=int(verdict.retry_in_s))
            return JobOutcome(claimed.job_id, "queued", stage=stage, error=exc.message)
        return JobOutcome(claimed.job_id, verdict.state, stage=stage, error=exc.message)

    # -------------------------------------------------------------- recording

    def _record(self, ctx, claimed, plan, outcome: JobOutcome, item_id: int, timeline) -> None:
        """Write the job row, detections and backups. Never fails the job."""
        from vidcleaner.pipeline import probe as probe_stage  # noqa: PLC0415
        from vidcleaner.pipeline import snippets as snippets_stage  # noqa: PLC0415
        from vidcleaner.pipeline import stt as stt_stage  # noqa: PLC0415
        from vidcleaner.pipeline import subtitles as subs_stage  # noqa: PLC0415
        from vidcleaner.pipeline import swap as swap_stage  # noqa: PLC0415
        from vidcleaner.pipeline.artifacts import DetectionResult  # noqa: PLC0415
        from vidcleaner.pipeline.persist import persist_run, persist_swap  # noqa: PLC0415

        if outcome.state == "queued":
            # A retry: `release` already set the state, and re-writing the job row
            # here would clear it.
            return
        try:
            probe = probe_stage.load(ctx.ws) if ctx.ws.probe_json.is_file() else None
            if probe is None:
                with session_scope() as session:
                    queue.set_state(
                        session,
                        job_id=claimed.job_id,
                        state=outcome.state,
                        stage=outcome.stage,
                        error=outcome.error,
                    )
                return

            detections = (
                DetectionResult.read(ctx.ws.detections_json)
                if ctx.ws.detections_json.is_file()
                else None
            )
            transcript = stt_stage.load(ctx.ws)
            subs = subs_stage.load(ctx.ws) if ctx.ws.subs_json.is_file() else None
            swap = swap_stage.load(ctx.ws)
            clips = snippets_stage.load(ctx.ws)

            with session_scope() as session:
                persist_run(
                    session,
                    plan.spec,
                    probe=probe,
                    detections=detections,
                    snippets=clips,
                    state=outcome.state,
                    stage=outcome.stage,
                    error=outcome.error,
                    model_used=transcript.model if transcript else None,
                    subtitle_source=subs.source.reason if subs else None,
                    timings=_timings(ctx.ws),
                    work_dir=ctx.ws.root,
                    media_item=session.get(type(plan.item), item_id),
                    count_attempt=False,
                )
                if swap is not None and outcome.state == "done":
                    persist_swap(
                        session,
                        swap,
                        media_item_id=item_id,
                        job_id=claimed.job_id,
                        retention_days=plan.settings.backup_retention_days,
                    )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail a good swap
            log.exception("job.persist_failed", job_id=claimed.job_id)
            timeline.error("could not record the run", error=str(exc)[:200])

        if outcome.state in ("done", "already_clean"):
            prune_work_dir(ctx.ws)


def _restore_before_reclean(session, job: Job, timeline) -> None:
    """Put the original back before re-cleaning a file we already cleaned.

    Without this, a reprocess reads *our own output* as its source: the previous
    Clean track becomes the new "Original", the file grows a track per run, and the
    sidecar subtitles were already redacted -- so §9.4's whole flow ("whitelist a
    false positive, reprocess, hear the word again") could never work. Found by the
    M4 demo, which stacked `Clean, Original, Original` on one episode.

    §6 words this as the audit pass "re-rendering from the backup original". Doing it
    by restoring first, rather than by pointing the pipeline at `/backups`, keeps the
    library-mutating code in exactly one place (`swap.py`, which owns `restore` too)
    and leaves one `kept` backup rather than a chain of them.

    Only for a job that will actually redo the work on the library: `force` (which
    the API sets for reprocess) and not `dry_run` (which must not touch anything).
    """
    from vidcleaner.db.models import Backup  # noqa: PLC0415
    from vidcleaner.pipeline.persist import restore_item  # noqa: PLC0415

    if not job.force or job.dry_run:
        return
    kept = session.scalars(
        select(Backup)
        .where(Backup.media_item_id == job.media_item_id, Backup.state == "kept")
        .order_by(Backup.created_at.desc(), Backup.id.desc())
    ).first()
    if kept is None:
        return
    try:
        report = restore_item(session, job.media_item_id)
    except (ValueError, RuntimeError, OSError) as exc:
        # The library file is still whatever it was; cleaning it again is worse than
        # not, so say so loudly and let the job run on it rather than failing here.
        timeline.warning("could not restore the original before re-cleaning", error=str(exc)[:200])
        return
    timeline.info(
        "restored the original before re-cleaning",
        path=report.restored_path,
        sidecars=report.sidecars,
    )


def _timings(ws) -> dict[str, float]:
    """From the stage markers, so a resumed job reports its whole history."""
    out: dict[str, float] = {}
    for stage in ws.completed_stages():
        marker = ws.read_marker(stage)
        if marker is not None:
            out[stage] = round(marker.elapsed_s, 3)
    return out


def _integrations_for(session) -> Any:
    from vidcleaner.integrations import from_database  # noqa: PLC0415

    bundle = from_database(session)
    if bundle.sonarr is None and bundle.radarr is None and bundle.jellyfin is None:
        bundle.close()
        return None
    return bundle


def _register_swap_reconciler() -> None:
    """Let stale recovery resolve a job killed mid-swap.

    Registered here rather than in ``claim`` so the queue -- which the api imports to
    enqueue from webhooks -- never has to import the pipeline.
    """
    if queue.SWAP_RECONCILER is not None:
        return
    from vidcleaner.pipeline.swap import reconcile  # noqa: PLC0415

    queue.SWAP_RECONCILER = reconcile


# ------------------------------------------------------------- timeline lines


def _log_transcript(timeline: Timeline, transcript) -> None:
    if transcript is None:
        return
    if not transcript.word_count and transcript.mode_reason == "full_skipped_too_long":
        # The one case where "0 detections" must not be read as "this file is clean".
        timeline.warning(
            "speech-to-text skipped: no usable subtitles and too long for a full pass",
            hint="raise stt_full_max_hours or run with --stt-mode full",
        )
        return
    timeline.info(
        "transcribed",
        mode=transcript.mode,
        model=transcript.model,
        windows=len(transcript.windows),
        words=transcript.word_count,
    )


def _log_detections(timeline: Timeline, detections) -> None:
    if detections is None:
        return
    stats = detections.stats
    timeline.info(
        "detected",
        detections=stats.get("detections", 0),
        muted=stats.get("muted", 0),
        suspicious=stats.get("suspicious", 0),
        seconds=round(detections.total_muted_s, 1),
    )


def _log_swap(timeline: Timeline, swap) -> None:
    if swap is None:
        return
    timeline.info("swapped", final=swap.final_path, backup=swap.backup_path)
    for warning in swap.warnings:
        timeline.warning("swap warning", detail=warning)


def _log_refresh(timeline: Timeline, refresh) -> None:
    if refresh is None:
        return
    timeline.info(
        "refreshed",
        arr=refresh.arr,
        command=refresh.arr_command_id,
        skipped=refresh.skipped or None,
    )
    for warning in refresh.warnings:
        timeline.warning("refresh warning", detail=warning)
