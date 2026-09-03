"""§6's audit pass: who gets audited, and when it re-renders.

Database only -- no ffmpeg, no torch, no stages. The audit's *correctness* is
`test_audit_compare.py`'s subject and its *execution* is `test_worker_run.py`'s; this
file pins the eligibility rules, every one of which exists to stop the scheduler
enqueueing work that cannot succeed or would repeat itself forever.

Through M4 this pass was inert: `enqueue_audit_pass` omitted `force`, so every job died
at `probe` with `already_clean`, and `_has_audit` then blocked that item for the
lifetime of the database.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from tests.support.library import add_backup, add_detections, add_job, make_movie, make_series
from vidcleaner.db.models import Backup, Job, MediaItem
from vidcleaner.db.session import session_scope
from vidcleaner.matching.profile import ensure_seed_data, matcher_for
from vidcleaner.settings_store import save_settings
from vidcleaner.worker.scheduler import enqueue_audit_pass, promote_audits


@pytest.fixture
def seeded(migrated):
    ensure_seed_data()
    return migrated


def snapshot(profile_hash: str) -> str:
    return json.dumps({"profile_hash": profile_hash})


def set_mode(mode: str, **extra) -> None:
    with session_scope() as session:
        save_settings(session, {"audit_pass": mode, **extra})


def current_hash(item_id: int) -> str:
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        return matcher_for(session, title_id=item.title_id, item_id=item.id).profile_hash


def cleaned_episode(tmp_path, *, hours: float = 0.5, mode: str = "windowed", arr_id: int = 1):
    """An episode we have cleaned, with a `kept` backup that exists on disk."""
    _title_id, episodes = make_series(f"Show {arr_id}", arr_id=arr_id, episodes=1, enabled=True)
    item_id = episodes[0]
    backup_file = tmp_path / "original.mkv"
    backup_file.write_bytes(b"original")

    job_id = add_job(item_id, state="done", trigger="backfill", stt_mode=mode, is_last=True)
    add_detections(job_id, item_id, "shit")
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        item.status = "clean"
        item.duration = hours * 3600.0
    add_backup(item_id, job_id=job_id, backup_path=str(backup_file), sha1_prefix="abc123")
    return item_id, job_id, backup_file


def audits_of(item_id: int) -> list[Job]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(Job)
                .where(Job.media_item_id == item_id, Job.trigger == "audit")
                .order_by(Job.created_at)
            ).all()
        )


# ------------------------------------------------------------------ the setting


def test_off_enqueues_nothing(seeded, tmp_path) -> None:
    cleaned_episode(tmp_path)
    set_mode("off")
    assert enqueue_audit_pass() == []


def test_idle_waits_for_an_empty_queue(seeded, tmp_path) -> None:
    """Not redundant with the idle poll that reaches the scheduler: a job can be
    `queued` with a future `retry_at`, or claimed by another worker."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    add_job(item_id, state="queued", trigger="manual")
    set_mode("idle")
    assert enqueue_audit_pass() == []


def test_always_enqueues_even_with_work_queued(seeded, tmp_path) -> None:
    """Otherwise `always` is indistinguishable from `idle`. Priority 900 still keeps
    the audit strictly last, so this cannot starve anything."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    _title, other = make_movie(arr_id=9)
    add_job(other, state="queued", trigger="manual")
    set_mode("always")

    assert len(enqueue_audit_pass()) == 1
    (audit,) = audits_of(item_id)
    assert audit.priority == 900
    assert audit.dry_run is True, "phase 1 must not touch the library"
    assert audit.stt_mode == "audit"
    assert audit.force is False, "the backup carries no tag, so force is unnecessary"


# -------------------------------------------------------------- who is eligible


def test_a_disabled_title_is_skipped(seeded, tmp_path) -> None:
    item_id, _job, _backup = cleaned_episode(tmp_path)
    with session_scope() as session:
        item = session.get(MediaItem, item_id)
        session.get(type(item).__mro__[0], item_id)
        from vidcleaner.db.models import Title

        session.get(Title, item.title_id).enabled = False
    set_mode("always")
    assert enqueue_audit_pass() == []


def test_an_item_whose_last_run_was_already_full_is_skipped(seeded, tmp_path) -> None:
    """A full pass over a file that already had one finds the same thing, at the
    same cost."""
    cleaned_episode(tmp_path, mode="full")
    set_mode("always")
    assert enqueue_audit_pass() == []


def test_an_item_with_no_backup_is_skipped(seeded, tmp_path) -> None:
    """§6's premise is re-checking the *original* audio, and after a clean the library
    file's default stream is the muted Clean track -- so there is nothing to audit."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    with session_scope() as session:
        session.get(Backup, 1).state = "purged"
    set_mode("always")
    assert enqueue_audit_pass() == []
    assert audits_of(item_id) == []


def test_a_missing_backup_file_is_marked_purged_and_skipped(seeded, tmp_path) -> None:
    """Self-healing, and far cheaper than `reconcile_backups`, which rglobs the whole
    backups tree to learn the same thing about one row."""
    item_id, _job, backup_file = cleaned_episode(tmp_path)
    backup_file.unlink()
    set_mode("always")

    assert enqueue_audit_pass() == []
    with session_scope() as session:
        assert session.get(Backup, 1).state == "purged"
    assert audits_of(item_id) == []


def test_a_film_longer_than_the_cap_is_skipped(seeded, tmp_path) -> None:
    """M2 exempted explicit modes from `stt_full_max_hours` so the cap could not stop
    a user who asked. Nobody asked here -- the scheduler volunteered -- and §13 lists
    a ~6-hour audit of a 3-hour film as the top CPU risk."""
    cleaned_episode(tmp_path, hours=4.0)
    set_mode("always", stt_full_max_hours=3.0)
    assert enqueue_audit_pass() == []


def test_only_one_item_is_enqueued_per_tick(seeded, tmp_path) -> None:
    cleaned_episode(tmp_path)
    other = tmp_path / "second"
    other.mkdir()
    cleaned_episode(other, arr_id=2)
    set_mode("always")
    assert len(enqueue_audit_pass()) == 1


# ------------------------------------------------------------ the re-audit key


def test_a_completed_audit_is_not_repeated(seeded, tmp_path) -> None:
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    add_job(
        item_id,
        state="done",
        trigger="audit",
        dry_run=True,
        source_fingerprint="abc123",
        profile_snapshot_json=snapshot(current_hash(item_id)),
    )
    assert enqueue_audit_pass() == []


def test_a_word_list_edit_earns_a_fresh_audit(seeded, tmp_path) -> None:
    """The profile hash is part of the key, so the same audio is re-checked against
    the new rules."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    add_job(
        item_id,
        state="done",
        trigger="audit",
        dry_run=True,
        source_fingerprint="abc123",
        profile_snapshot_json=snapshot("v1:stale"),
    )
    assert len(enqueue_audit_pass()) == 1


def test_a_replaced_file_earns_a_fresh_audit(seeded, tmp_path) -> None:
    """A Sonarr upgrade means a different backup, so a different fingerprint."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    add_job(
        item_id,
        state="done",
        trigger="audit",
        dry_run=True,
        source_fingerprint="OLDFILE",
        profile_snapshot_json=snapshot(current_hash(item_id)),
    )
    assert len(enqueue_audit_pass()) == 1


def test_a_poison_file_stops_being_retried(seeded, tmp_path) -> None:
    """M4's `_has_audit` gave one attempt forever; this gives two failures."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    for _ in range(2):
        add_job(item_id, state="failed", trigger="audit", dry_run=True)
    assert enqueue_audit_pass() == []


def test_one_failure_does_not_block_a_retry(seeded, tmp_path) -> None:
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    add_job(item_id, state="failed", trigger="audit", dry_run=True)
    assert len(enqueue_audit_pass()) == 1


# ----------------------------------------------------------------- promotion


def audit_with(item_id: int, *words: str, hash_: str | None = None) -> str:
    """A completed phase 1 whose rows are the **merged** set it persisted."""
    job_id = add_job(
        item_id,
        state="done",
        trigger="audit",
        dry_run=True,
        source_fingerprint="abc123",
        profile_snapshot_json=snapshot(hash_ or current_hash(item_id)),
    )
    add_detections(job_id, item_id, *words)
    return job_id


def test_an_audit_that_found_nothing_new_does_not_re_render(seeded, tmp_path) -> None:
    """The common case, and the whole reason phase 1 is separate: an arr rescan and a
    Jellyfin refresh per audited file would be pure churn."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    audit_with(item_id, "shit")  # the same word the clean run already muted
    assert promote_audits() == []


def test_a_new_hit_earns_a_forced_re_render(seeded, tmp_path) -> None:
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    audit_with(item_id, "shit", "fuck")  # `fuck` is new

    assert len(promote_audits()) == 1
    promotion = [j for j in audits_of(item_id) if not j.dry_run]
    assert len(promotion) == 1
    assert promotion[0].force is True, "it has to restore the original first"
    assert promotion[0].priority == 900


def test_promotion_is_idempotent(seeded, tmp_path) -> None:
    """Recomputed rather than remembered, so running it twice must enqueue once."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    audit_with(item_id, "shit", "fuck")

    assert len(promote_audits()) == 1
    assert promote_audits() == [], "the live job blocks a second"


def test_promotion_stops_once_the_re_render_has_run(seeded, tmp_path) -> None:
    """It terminates because phase 2 becomes the evidence job with the merged set."""
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always")
    audit = audit_with(item_id, "shit", "fuck")

    rendered = add_job(item_id, state="done", trigger="audit", is_last=True)
    add_detections(rendered, item_id, "shit", "fuck")
    with session_scope() as session:
        session.get(Job, rendered).created_at = session.get(Job, audit).created_at.replace(
            microsecond=0
        )
    assert promote_audits() == []


def test_promotion_respects_the_off_switch(seeded, tmp_path) -> None:
    item_id, _job, _backup = cleaned_episode(tmp_path)
    audit_with(item_id, "shit", "fuck")
    set_mode("off")
    assert promote_audits() == []


def test_the_confidence_floor_can_veto_a_promotion(seeded, tmp_path) -> None:
    item_id, _job, _backup = cleaned_episode(tmp_path)
    set_mode("always", audit_min_confidence=0.95)
    audit_with(item_id, "shit", "fuck")  # add_detections uses confidence 0.9
    assert promote_audits() == []
