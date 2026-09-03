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
from vidcleaner.db.queries import evidence_job_ids
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
        self._paused_reason: str | None = None
        """Set while the disk gate is holding the queue, so the log says it once."""
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
        paused = self._disk_pause()
        if paused is not None:
            # Deliberately before `claim_next`: the per-job guard in `_after_probe`
            # is right but arrives too late to be kind. A full `/work` would fail
            # three hundred backfill jobs one at a time, burning an attempt each and
            # filling §9.1's Queue page with identical failures, when the honest
            # answer is "the disk is full, nothing can run". This pauses instead, so
            # the queue survives intact and drains once space appears.
            if paused != self._paused_reason:
                log.warning("worker.paused", reason=paused)
                self._paused_reason = paused
            return False
        if self._paused_reason is not None:
            log.info("worker.resumed", after=self._paused_reason)
            self._paused_reason = None

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

    def _disk_pause(self) -> str | None:
        """Why the queue should not claim anything right now, if it should not."""
        import shutil  # noqa: PLC0415

        from vidcleaner.settings_store import load_settings  # noqa: PLC0415

        try:
            with session_scope() as session:
                floor_gib = load_settings(session).min_free_gib
        except Exception:  # noqa: BLE001 - a settings read must never stop the worker
            return None
        if floor_gib <= 0:
            return None
        try:
            free = shutil.disk_usage(self.settings.work_dir).free
        except OSError as exc:
            return f"cannot read free space on {self.settings.work_dir} ({exc.strerror or exc})"
        if free < floor_gib * 2**30:
            return f"/work has {free / 2**30:.1f} GiB free, below the {floor_gib:g} GiB floor"
        return None

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
            try:
                if did_work:
                    # `audit_pass="always"` means "enqueue regardless of queue depth".
                    # Reaching the scheduler only on the idle path made it identical
                    # to `"idle"`, which is not what the setting says. Priority 900
                    # still keeps an audit strictly last, so this cannot starve
                    # anything -- it only lets the row exist sooner.
                    self.scheduler.tick(only=("audit",))
                else:
                    # Everything else runs only when the queue is idle, which is what
                    # §6 requires of the audit pass and costs the rest nothing.
                    self.scheduler.tick()
            except Exception:
                log.exception("worker.scheduler_failed")
            if not did_work:
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
            restored = _restore_before_reclean(session, job, timeline)
            if restored == "failed" and job.trigger == "audit":
                # Unattended, so there is no one to notice a second Clean track being
                # rendered over the first. The library is untouched; stop here. The
                # state is written now because this returns before `_record` -- which
                # needs a work dir and a probe this job never got as far as making.
                error = "could not restore the original before the audit re-render"
                timeline.error("audit promotion abandoned", reason=error)
                queue.set_state(
                    session, job_id=claimed.job_id, state="failed", stage="swap", error=error
                )
                timeline.flush()
                return JobOutcome(claimed.job_id, "failed", stage="swap", error=error)
            plan = plan_job(session, job, deploy=self.settings)
            item_id = plan.item.id
            arr_paths = (plan.title.arr_path,) if plan.title and plan.title.arr_path else ()
            integrations = _integrations_for(session)

        if plan.invalidated:
            timeline.warning("work dir discarded", reason=plan.invalidated)

        stages = _stages_for(plan)
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

        if plan.spec.trigger == "audit" and not plan.spec.dry_run:
            _seed_audit_detections(ctx, plan, timeline)

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
                if plan.spec.trigger == "audit" and plan.spec.dry_run:
                    self._after_audit_detect(ctx, plan, result.result, timeline)
            elif stage == "swap":
                _log_swap(timeline, result.result)
            elif stage == "refresh":
                _log_refresh(timeline, result.result)

        return JobOutcome(claimed.job_id, "done")

    def _after_audit_detect(self, ctx, plan, found, timeline) -> None:
        """Turn the audit's STT-only findings into §6's **union**, and record that.

        ``detect(mode="audit")`` ignores subtitle hits entirely, and M2 measured what
        that costs: full mode found 40 detections against windowed's 49, missing 22 of
        them. Rendering from ``found`` alone would therefore make the file *worse*.
        §6 says the audit "adds any detections the subtitles missed", so the result
        written back here is ``prior | found`` -- and because `_record` reads
        ``detections.json`` from disk, overwriting it is what makes the merged set the
        one that reaches the database.
        """
        from vidcleaner.pipeline.audit import (  # noqa: PLC0415
            AuditOptions,
            compare,
            merged_result,  # noqa: PLC0415
        )
        from vidcleaner.pipeline.detect import DetectOptions  # noqa: PLC0415
        from vidcleaner.pipeline.persist import detections_for_job  # noqa: PLC0415

        with session_scope() as session:
            item = session.get(type(plan.item), plan.item.id)
            evidence = evidence_job_ids(session, [item]).get(item.id) if item else None
            prior = detections_for_job(session, evidence) if evidence else []

        comparison = compare(
            prior,
            found.detections,
            opts=AuditOptions(min_confidence=plan.settings.audit_min_confidence),
        )
        merged = merged_result(
            comparison,
            profile_hash=plan.spec.profile_hash,
            duration_s=0.0,
            detect_opts=DetectOptions.from_profile(plan.spec.profile),
        )
        merged.write(ctx.ws.detections_json)
        timeline.info(
            "audited against the backup original",
            evidence_job=evidence,
            **comparison.summary,
            will_re_render=comparison.should_render,
        )

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
                    # Phase 1 observes the backup and changes nothing on disk, so the
                    # item stays `clean`. Letting it fall to `pending` would drop it
                    # out of `sync.CLEAN_STATUSES` and have the hourly sync re-enqueue
                    # a perfectly clean file forever -- the loop §6's audit must not
                    # start. `last_job_id` still moves, which is what closes it.
                    preserve_item_status=(plan.spec.trigger == "audit" and plan.spec.dry_run),
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


def _seed_audit_detections(ctx, plan, timeline) -> None:
    """Hand the promotion the merged set phase 1 already computed.

    Phase 1 spent ~30 minutes transcribing the whole file and persisted the merge; the
    promotion must not pay that again to render the very same ranges. `seed_detections`
    writes ``detections.json`` and marks `transcribe`/`detect` done, so `run_stage`
    skips them -- the seam `vidcleaner clean --detections` has used since M1.

    `extract` is deliberately left to run (``skip_extract=False``) because `snippets`
    cuts from ``audio.wav``.
    """
    from vidcleaner.pipeline.audit import AuditComparison, merged_result  # noqa: PLC0415
    from vidcleaner.pipeline.detect import DetectOptions  # noqa: PLC0415
    from vidcleaner.pipeline.persist import detections_for_job  # noqa: PLC0415
    from vidcleaner.pipeline.stages import seed_detections  # noqa: PLC0415

    with session_scope() as session:
        audit_job = _newest_audit_evidence(session, plan.item.id)
        if audit_job is None:
            timeline.warning("no audit evidence to render from")
            return
        detections = detections_for_job(session, audit_job)

    result = merged_result(
        AuditComparison(carried=tuple(detections)),
        profile_hash=plan.spec.profile_hash,
        duration_s=0.0,
        detect_opts=DetectOptions.from_profile(plan.spec.profile),
    )
    seed_detections(ctx, result, skip_extract=False)
    timeline.info(
        "rendering from the audit's detections",
        source_job=audit_job,
        detections=len(result.detections),
        ranges=len(result.mute_ranges),
    )


def _newest_audit_evidence(session, media_item_id: int) -> str | None:
    """The most recent completed audit phase 1 for this item."""
    return session.scalars(
        select(Job.id)
        .where(
            Job.media_item_id == media_item_id,
            Job.trigger == "audit",
            Job.dry_run.is_(True),
            Job.state == "done",
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
    ).first()


def _stages_for(plan) -> list[str]:
    """Which stages this job runs.

    Three shapes. A dry run stops after `detect` -- and M5's audit **phase 1** is a
    dry run, which is what keeps the library byte-identical while a 30-minute
    transcription runs. An audit **promotion** already holds its detection set (seeded
    from the database by :func:`_seed_audit_detections`) and so skips `transcribe` and
    `detect` entirely: ~27 s of render and swap instead of another full pass. Everything
    else runs the lot.
    """
    if plan.spec.dry_run:
        return list(DRY_RUN_STAGES)
    if plan.spec.trigger == "audit":
        # `extract` stays: `snippets` cuts its review clips out of `audio.wav`, so
        # skipping it would leave every detection on the Item page without audio.
        return [s for s in M4_STAGES if s not in {"transcribe", "detect"}]
    return list(M4_STAGES)


def _restore_before_reclean(session, job: Job, timeline) -> str:
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

    Returns ``"skipped"``, ``"restored"`` or ``"failed"``. The caller cares because a
    failed restore is only tolerable for a *user-clicked* reprocess: for an unattended
    audit promotion, running on anyway would render a second Clean track over the first
    -- the M4 demo's bug -- which is precisely what §6's "never re-encodes the clean
    track twice" forbids.
    """
    from vidcleaner.db.models import Backup  # noqa: PLC0415
    from vidcleaner.pipeline.persist import restore_item  # noqa: PLC0415

    if not job.force or job.dry_run:
        return "skipped"
    kept = session.scalars(
        select(Backup)
        .where(Backup.media_item_id == job.media_item_id, Backup.state == "kept")
        .order_by(Backup.created_at.desc(), Backup.id.desc())
    ).first()
    if kept is None:
        return "skipped"
    try:
        report = restore_item(session, job.media_item_id)
    except (ValueError, RuntimeError, OSError) as exc:
        # The library file is still whatever it was; cleaning it again is worse than
        # not, so say so loudly and let the caller decide.
        timeline.warning("could not restore the original before re-cleaning", error=str(exc)[:200])
        return "failed"
    timeline.info(
        "restored the original before re-cleaning",
        path=report.restored_path,
        sidecars=report.sidecars,
    )
    return "restored"


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
