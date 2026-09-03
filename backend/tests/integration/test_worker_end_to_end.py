"""The worker driving a real file through real ffmpeg -- PLAN.md §11's M3 demo core.

STT is replayed from a transcript (`ScriptedTranscriber`, which ships in production
for `--transcript`), because recognition quality is M2's subject and this is about the
worker: claiming, the stage machine, the real swap, the backups row, and resume after
the process dies mid-job.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from vidcleaner.config import Settings
from vidcleaner.db.models import Backup, Detection, Job, JobLog, MediaItem, Title
from vidcleaner.db.session import session_scope
from vidcleaner.matching.profile import ensure_seed_data
from vidcleaner.pipeline.artifacts import (
    TimeRange,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from vidcleaner.pipeline.stt import ScriptedTranscriber
from vidcleaner.pipeline.workspace import Workspace
from vidcleaner.worker.claim import enqueue
from vidcleaner.worker.runner import Worker

WORDS = [
    ("Oh", 0.55, 0.65),
    ("shit", 0.70, 0.95),
    ("You", 2.05, 2.15),
    ("fucking", 2.20, 2.65),
    ("idiot", 2.70, 2.95),
    ("God", 5.05, 5.25),
    ("damn", 5.30, 5.60),
    ("it", 5.65, 5.80),
]


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcript.json"
    Transcript(
        mode="windowed",
        model="scripted",
        segments=[
            TranscriptSegment(
                start=WORDS[0][1],
                end=WORDS[-1][2],
                text=" ".join(w for w, _, _ in WORDS),
                words=[TranscriptWord(word=w, start=s, end=e) for w, s, e in WORDS],
            )
        ],
    ).write(path)
    return path


@pytest.fixture
def queued(migrated: Settings, sample_mkv: Path):
    """The fixture, inside `/media`, tracked and queued."""
    ensure_seed_data()
    folder = migrated.media_dir / "tv" / "Show"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "S01E01.mkv"
    sample_mkv.rename(source)

    with session_scope() as session:
        title = Title(kind="series", arr_id=42, title="Show", enabled=True)
        session.add(title)
        session.flush()
        item = MediaItem(
            title_id=title.id,
            kind="episode",
            season=1,
            episode=1,
            path=str(source),
            status="pending",
        )
        session.add(item)
        session.flush()
        job_id = enqueue(session, media_item_id=item.id, trigger="manual").job_id
        return job_id, item.id, source


def worker_for(settings: Settings, transcript: Path) -> Worker:
    return Worker(settings, poll_interval=0.01, transcriber=ScriptedTranscriber(transcript))


def test_the_worker_cleans_a_real_file_and_swaps_it_in(migrated, queued, transcript) -> None:
    job_id, item_id, source = queued
    original = source.read_bytes()

    assert worker_for(migrated, transcript).poll_once() is True

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.state == "done", job.error
        assert job.progress_pct == 100.0
        assert job.claimed_by is None
        assert set(json.loads(job.timings_json)) >= {"probe", "render", "verify", "swap"}

        detections = session.scalars(select(Detection)).all()
        assert detections, "the scripted transcript contains profanity"

        backup = session.scalars(select(Backup)).one()
        assert backup.state == "kept"
        assert Path(backup.backup_path).read_bytes() == original
        assert backup.sha1_prefix

        item = session.get(MediaItem, item_id)
        assert item.status == "clean" and item.last_job_id == job_id

    # The library holds the cleaned file, and only it.
    assert source.is_file() and source.read_bytes() != original
    assert sorted(p.name for p in source.parent.iterdir()) == ["S01E01.mkv"]


def test_the_cleaned_file_really_has_two_audio_tracks(migrated, queued, transcript) -> None:
    from vidcleaner.pipeline.ffmpeg import FFmpegRunner

    job_id, item_id, source = queued
    worker_for(migrated, transcript).poll_once()

    data = FFmpegRunner().probe(source)
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    assert audio[0]["tags"]["title"] == "Clean"
    assert audio[0]["disposition"]["default"] == 1
    assert audio[1]["tags"]["title"] == "Original"
    assert data["format"]["tags"]["VIDCLEANER"] == "1"


def test_the_work_dir_is_pruned_but_keeps_its_evidence(migrated, queued, transcript) -> None:
    job_id, item_id, source = queued
    worker_for(migrated, transcript).poll_once()

    ws = Workspace.for_job(job_id, migrated)
    assert not ws.out_mkv.exists(), "source-sized, and /work is a cache disk"
    assert not ws.audio_wav.exists()
    assert ws.detections_json.is_file() and ws.probe_json.is_file()
    assert ws.swap_json.is_file() and ws.swap_plan_json.is_file()


def test_a_second_run_reports_already_clean(migrated, queued, transcript) -> None:
    """§4's idempotency loop, through the worker and a real swap."""
    job_id, item_id, source = queued
    worker = worker_for(migrated, transcript)
    worker.poll_once()

    with session_scope() as session:
        second = enqueue(session, media_item_id=item_id, trigger="reprocess").job_id
    assert worker.poll_once() is True
    with session_scope() as session:
        assert session.get(Job, second).state == "already_clean"


def test_killing_the_worker_mid_job_resumes_from_the_markers(
    migrated, queued, transcript, monkeypatch
) -> None:
    """§4: "on startup, jobs with a stale heartbeat resume from the last completed
    stage marker". `swapping` is deliberately excluded, so this stops before it.

    `_Killed` is a `BaseException`, which is what makes it a kill rather than an
    error: `poll_once` catches `Exception` and releases the job, so an ordinary
    exception would never leave the claim behind the way SIGKILL does.
    """
    job_id, item_id, source = queued
    original = source.read_bytes()

    import vidcleaner.pipeline.stages as stages_mod

    real_run_stage = stages_mod.run_stage

    def die_after_detect(ctx, name, **kw):
        outcome = real_run_stage(ctx, name, **kw)
        if name == "detect":
            raise _Killed(name)
        return outcome

    monkeypatch.setattr(stages_mod, "run_stage", die_after_detect)
    with pytest.raises(_Killed):
        worker_for(migrated, transcript).poll_once()
    monkeypatch.undo()

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.claimed_by is not None, "a killed worker leaves its claim behind"
        assert job.state == "detecting"
        job.heartbeat = None  # what a dead process's lapsed heartbeat looks like

    ws = Workspace.for_job(job_id, migrated)
    assert ws.is_done("probe") and ws.is_done("detect")
    assert source.read_bytes() == original, "nothing touched the library yet"

    # A fresh worker recovers the claim and finishes the job.
    fresh = worker_for(migrated, transcript)
    with session_scope() as session:
        from vidcleaner.worker.claim import recover_stale

        assert [s.outcome for s in recover_stale(session)] == ["requeued"]
    assert fresh.poll_once() is True

    with session_scope() as session:
        assert session.get(Job, job_id).state == "done"
        assert session.scalars(select(Backup)).one().state == "kept"
    assert source.read_bytes() != original
    messages = [row.msg for row in _job_logs(job_id)]
    assert any("skipped" in m for m in messages), "the completed stages were skipped"
    assert any("recovered from detecting" in m for m in messages)


class _Killed(BaseException):
    """Stands in for SIGKILL: outside the `Exception` hierarchy, so the runner's own
    error handling does not turn it into a graceful release."""


def _job_logs(job_id: str):
    with session_scope() as session:
        rows = session.scalars(
            select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.id)
        ).all()
        for row in rows:
            session.expunge(row)
        return rows


def test_the_run_loop_drains_the_queue(migrated, queued, transcript) -> None:
    job_id, item_id, source = queued
    worker = worker_for(migrated, transcript)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            with session_scope() as session:
                if session.get(Job, job_id).state == "done":
                    break
            time.sleep(0.05)
    finally:
        worker.request_stop()
        thread.join(10)
    with session_scope() as session:
        assert session.get(Job, job_id).state == "done"


# ------------------------------------------------- §6's audit pass, on real audio


@pytest.fixture
def audit_transcript(tmp_path: Path) -> Path:
    """What a full-file pass hears: everything the windowed run heard, **plus** the
    `bullshit` at 8 s that no subtitle cue ever named.

    This is §6's premise made concrete -- background or crowd dialogue the subtitles
    omit -- and it is what M2 measured when full mode found 13 detections the windowed
    run had missed.
    """
    extra = [*WORDS, ("bullshit", 8.10, 8.55)]
    path = tmp_path / "audit-transcript.json"
    Transcript(
        mode="audit",
        model="scripted-audit",
        segments=[
            TranscriptSegment(
                start=extra[0][1],
                end=extra[-1][2],
                text=" ".join(w for w, _, _ in extra),
                words=[TranscriptWord(word=w, start=s, end=e) for w, s, e in extra],
            )
        ],
    ).write(path)
    return path


def audit_enqueue(item_id: int, *, promotion: bool = False) -> str:
    with session_scope() as session:
        return enqueue(
            session,
            media_item_id=item_id,
            trigger="audit",
            stt_mode="audit",
            dry_run=not promotion,
            force=promotion,
        ).job_id


def words_of(job_id: str) -> list[str]:
    with session_scope() as session:
        return [
            d.word_canonical
            for d in session.scalars(
                select(Detection).where(Detection.job_id == job_id).order_by(Detection.start_s)
            )
        ]


def test_the_audit_finds_a_word_the_subtitles_missed_and_re_renders_it(
    migrated, queued, transcript, audit_transcript, runner
) -> None:
    """§6's audit pass end to end on real media, and the assertion that matters.

    ``detect(mode="audit")`` is STT-only. If the promotion rendered from *that* set
    instead of the union, the words the first run muted would come back audible -- M2
    measured full mode missing 22 of windowed's 49 detections, so this is not a
    hypothetical. The test therefore checks **both** ends: the new word goes silent,
    and an original one stays silent.
    """
    job_id, item_id, source = queued
    assert worker_for(migrated, transcript).poll_once() is True
    cleaned_bytes = source.read_bytes()
    first_words = words_of(job_id)
    assert "shit" in first_words and "bullshit" not in first_words

    # `bullshit` at 8.1 s is audible after the first clean: nothing muted it.
    before = runner.measure_volume(source, stream="0:a:0", window=TimeRange(start=8.15, end=8.50))
    assert before is not None and before.max_db > -50.0

    # --- phase 1: detect against the backup, change nothing -------------------
    audit_one = audit_enqueue(item_id)
    assert worker_for(migrated, audit_transcript).poll_once() is True

    with session_scope() as session:
        job = session.get(Job, audit_one)
        assert job.state == "done", job.error
        assert job.dry_run is True
        item = session.get(MediaItem, item_id)
        assert item.status == "clean", "an observation must not disturb the status"
        assert item.last_job_id == audit_one
    assert source.read_bytes() == cleaned_bytes, "the library file is byte-identical"

    merged = words_of(audit_one)
    assert "bullshit" in merged, "the audit heard the missed word"
    assert "shit" in merged, "and kept what the file already had -- §6's union"

    # --- phase 2: restore, re-render from the merged set, swap ----------------
    audit_two = audit_enqueue(item_id, promotion=True)
    assert worker_for(migrated, audit_transcript).poll_once() is True

    with session_scope() as session:
        job = session.get(Job, audit_two)
        assert job.state == "done", job.error
    assert source.read_bytes() != cleaned_bytes, "the file was re-rendered"

    after = runner.measure_volume(source, stream="0:a:0", window=TimeRange(start=8.15, end=8.50))
    assert after is not None
    assert after.max_db <= -80.0, "the newly found word is muted"

    # The half a naive implementation gets wrong: the original mutes must survive.
    original_word = runner.measure_volume(
        source, stream="0:a:0", window=TimeRange(start=2.30, end=2.60)
    )
    assert original_word is not None
    assert original_word.max_db <= -80.0, "'fucking' is still muted"

    # A control window, the tripwire §12 relies on: not everything went silent.
    # The fixture is 10 s long and the last mute ends at ~8.67 s, so this is clear.
    control = runner.measure_volume(source, stream="0:a:0", window=TimeRange(start=9.0, end=9.8))
    assert control is not None and control.max_db > -50.0

    with session_scope() as session:
        states = [b.state for b in session.scalars(select(Backup)).all()]
    assert states.count("kept") == 1, "restore-then-swap leaves one original"


def test_an_audit_that_confirms_the_file_enqueues_no_re_render(
    migrated, queued, transcript
) -> None:
    """The common case. An arr rescan and a Jellyfin refresh per audited file would be
    pure churn, so `promote_audits` has to find nothing to do here."""
    from vidcleaner.settings_store import save_settings
    from vidcleaner.worker.scheduler import promote_audits

    _job_id, item_id, source = queued
    assert worker_for(migrated, transcript).poll_once() is True
    cleaned = source.read_bytes()

    # The audit hears exactly what the first pass did.
    audit_enqueue(item_id)
    assert worker_for(migrated, transcript).poll_once() is True

    with session_scope() as session:
        save_settings(session, {"audit_pass": "always"})
    assert promote_audits() == []
    assert source.read_bytes() == cleaned
