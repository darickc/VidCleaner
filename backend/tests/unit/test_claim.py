"""The queue: claiming, fencing, staleness and enqueue policy (PLAN.md §4/§6.0).

No ffmpeg and no torch. The `migrated` fixture is a **file**-backed SQLite database,
which matters for the concurrency tests: SQLAlchemy only passes
``check_same_thread=False`` and uses ``QueuePool`` for file URLs, so an in-memory URL
would flip to ``SingletonThreadPool`` and break every thread here.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from vidcleaner.config import Settings
from vidcleaner.db.models import Job, JobLog, MediaItem, Title
from vidcleaner.db.session import session_scope, utcnow
from vidcleaner.worker import claim as claim_mod
from vidcleaner.worker.claim import (
    MAX_ATTEMPTS,
    cancel,
    claim_next,
    enqueue,
    heartbeat,
    recover_stale,
    release,
    reprioritize,
    should_abort,
)


def make_items(count: int = 1) -> list[int]:
    with session_scope() as session:
        title = Title(kind="series", arr_id=1, title="Show", enabled=True)
        session.add(title)
        session.flush()
        ids = []
        for n in range(count):
            item = MediaItem(
                title_id=title.id,
                kind="episode",
                season=1,
                episode=n + 1,
                path=f"/media/tv/Show/S01E{n + 1:02d}.mkv",
                status="pending",
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
        return ids


def add_job(item_id: int, **kwargs) -> str:
    defaults = {
        "id": kwargs.pop("id", f"job-{item_id}"),
        "media_item_id": item_id,
        "trigger": "manual",
        "state": "queued",
    }
    defaults.update(kwargs)
    with session_scope() as session:
        session.add(Job(**defaults))
    return str(defaults["id"])


# ------------------------------------------------------------------- ordering


def test_claims_by_priority_then_creation_order(migrated: Settings) -> None:
    items = make_items(3)
    now = utcnow()
    add_job(items[0], id="late-high", priority=10, created_at=now)
    add_job(items[1], id="early-low", priority=200, created_at=now - timedelta(hours=1))
    add_job(items[2], id="mid", priority=100, created_at=now)

    taken = []
    while (claim := claim_next(worker_id="w1", settings=migrated)) is not None:
        taken.append(claim.job_id)
    assert taken == ["late-high", "mid", "early-low"]


def test_an_empty_queue_claims_nothing(migrated: Settings) -> None:
    assert claim_next(worker_id="w1", settings=migrated) is None


def test_claiming_counts_an_attempt_and_stamps_the_worker(migrated: Settings) -> None:
    """`attempts` counts claims, so a worker killed mid-render still burns one."""
    item = make_items()[0]
    add_job(item, id="j")
    claim = claim_next(worker_id="w1", settings=migrated)
    assert claim is not None and claim.attempts == 1

    with session_scope() as session:
        job = session.get(Job, "j")
        assert job is not None
        assert job.state == "probing"
        assert job.claimed_by == "w1"
        assert job.attempts == 1
        assert job.heartbeat is not None
        assert job.started_at is not None


def test_a_claimed_job_is_not_claimed_again(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j")
    assert claim_next(worker_id="w1", settings=migrated) is not None
    assert claim_next(worker_id="w2", settings=migrated) is None


def test_concurrent_claimers_take_distinct_jobs(migrated: Settings) -> None:
    items = make_items(3)
    for n, item in enumerate(items):
        add_job(item, id=f"j{n}", priority=100)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda n: claim_next(worker_id=f"w{n}", settings=migrated),
                range(8),
            )
        )

    won = [c for c in claims if c is not None]
    # The property under test is that nobody claimed the same job twice. A claimer
    # that lost the write lock past its busy timeout legitimately gets nothing, so
    # the remainder is drained afterwards rather than asserted to be zero here.
    assert len({c.job_id for c in won}) == len(won)
    while (extra := claim_next(worker_id="drain", settings=migrated)) is not None:
        won.append(extra)
    assert len({c.job_id for c in won}) == 3
    with session_scope() as session:
        assert all(j.attempts == 1 for j in session.scalars(select(Job)))


# ------------------------------------------------------------------- retry_at


def test_retry_at_in_the_future_is_not_claimable(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j", retry_at=utcnow() + timedelta(minutes=5))
    assert claim_next(worker_id="w1", settings=migrated) is None


def test_retry_at_in_the_past_is_claimable_and_cleared(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j", retry_at=utcnow() - timedelta(seconds=1))
    assert claim_next(worker_id="w1", settings=migrated) is not None
    with session_scope() as session:
        assert session.get(Job, "j").retry_at is None


def test_release_schedules_the_backoff(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j")
    claim_next(worker_id="w1", settings=migrated)
    when = utcnow() + timedelta(seconds=60)
    with session_scope() as session:
        assert release(session, job_id="j", retry_at=when, error="sonarr timed out")
    assert claim_next(worker_id="w1", settings=migrated) is None
    with session_scope() as session:
        job = session.get(Job, "j")
        assert job.state == "queued" and job.claimed_by is None


# ------------------------------------------------------------------- heartbeat


def test_heartbeat_updates_progress(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j")
    claim_next(worker_id="w1", settings=migrated)
    assert heartbeat(
        job_id="j",
        worker_id="w1",
        state="transcribing",
        stage="transcribe",
        progress_pct=42.5,
        settings=migrated,
    )
    with session_scope() as session:
        job = session.get(Job, "j")
        assert (job.state, job.stage, job.progress_pct) == ("transcribing", "transcribe", 42.5)


def test_heartbeat_fails_when_the_job_was_stolen(migrated: Settings) -> None:
    """The fencing token. A `False` return means another process owns the job now."""
    item = make_items()[0]
    add_job(item, id="j")
    claim_next(worker_id="w1", settings=migrated)
    with session_scope() as session:
        session.get(Job, "j").claimed_by = "w2"
    assert heartbeat(job_id="j", worker_id="w1", settings=migrated) is False
    assert heartbeat(job_id="j", worker_id="w2", settings=migrated) is True


def test_should_abort_sees_a_cancellation(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j")
    claim_next(worker_id="w1", settings=migrated)
    with session_scope() as session:
        assert should_abort(session, "j") is False
        cancel(session, "j", reason="superseded")
    with session_scope() as session:
        assert should_abort(session, "j") is True
        assert should_abort(session, "no-such-job") is True


# -------------------------------------------------------------------- staleness


def test_a_stale_running_job_is_requeued(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j", state="transcribing", claimed_by="dead", heartbeat=utcnow())
    with session_scope() as session:
        assert recover_stale(session, now=utcnow() + timedelta(seconds=121)) == [
            claim_mod.StaleJob("j", "transcribing", "requeued")
        ]
    with session_scope() as session:
        job = session.get(Job, "j")
        assert job.state == "queued" and job.claimed_by is None


def test_a_live_running_job_is_left_alone(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j", state="transcribing", claimed_by="alive", heartbeat=utcnow())
    with session_scope() as session:
        assert recover_stale(session) == []
        assert session.get(Job, "j").state == "transcribing"


def test_a_running_job_with_no_heartbeat_is_stale(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j", state="rendering", claimed_by="dead", heartbeat=None)
    with session_scope() as session:
        assert [s.outcome for s in recover_stale(session)] == ["requeued"]


def test_an_interrupted_swap_is_never_requeued_blind(migrated: Settings) -> None:
    """The one non-idempotent stage: a blind retry can make the library worse."""
    item = make_items()[0]
    add_job(item, id="j", state="swapping", claimed_by="dead", heartbeat=None)
    with session_scope() as session:
        assert [s.outcome for s in recover_stale(session)] == ["failed"]
        job = session.get(Job, "j")
        assert job.state == "failed"
        assert "reconciliation" in (job.error or "")


def test_the_swap_reconciler_can_requeue(migrated: Settings, monkeypatch) -> None:
    item = make_items()[0]
    add_job(item, id="j", state="swapping", claimed_by="dead", heartbeat=None)
    monkeypatch.setattr(claim_mod, "SWAP_RECONCILER", lambda session, job: "requeue")
    with session_scope() as session:
        assert [s.outcome for s in recover_stale(session)] == ["requeued"]
        assert session.get(Job, "j").state == "queued"


def test_a_job_that_keeps_killing_the_worker_is_retired(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(
        item,
        id="j",
        state="transcribing",
        claimed_by="dead",
        heartbeat=None,
        attempts=MAX_ATTEMPTS,
    )
    with session_scope() as session:
        assert [s.outcome for s in recover_stale(session)] == ["retired"]
        assert session.get(Job, "j").state == "failed"


def test_recovery_writes_the_job_timeline(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j", state="detecting", claimed_by="dead", heartbeat=None)
    with session_scope() as session:
        recover_stale(session)
    with session_scope() as session:
        rows = session.scalars(select(JobLog).where(JobLog.job_id == "j")).all()
        assert [r.level for r in rows] == ["warning"]
        assert "recovered from detecting" in rows[0].msg


# --------------------------------------------------------------------- enqueue


def test_enqueue_uses_the_trigger_default_priority(migrated: Settings) -> None:
    items = make_items(2)
    with session_scope() as session:
        webhook = enqueue(session, media_item_id=items[0], trigger="webhook")
        backfill = enqueue(session, media_item_id=items[1], trigger="backfill")
    with session_scope() as session:
        assert session.get(Job, webhook.job_id).priority == 100
        assert session.get(Job, backfill.job_id).priority == 200
        assert session.get(MediaItem, items[0]).status == "queued"


def test_a_repeat_inside_the_dedupe_window_returns_the_same_job(migrated: Settings) -> None:
    item = make_items()[0]
    with session_scope() as session:
        first = enqueue(session, media_item_id=item, trigger="webhook")
        again = enqueue(session, media_item_id=item, trigger="webhook")
    assert again.job_id == first.job_id
    assert (again.created, again.reason) == (False, "deduped")


def test_a_repeat_outside_the_window_still_finds_the_live_job(migrated: Settings) -> None:
    """The window only changes the *reason*: one live job per item is the invariant."""
    item = make_items()[0]
    with session_scope() as session:
        first = enqueue(session, media_item_id=item, trigger="webhook")
        later = enqueue(
            session,
            media_item_id=item,
            trigger="webhook",
            now=utcnow() + timedelta(seconds=120),
        )
    assert later.job_id == first.job_id
    assert (later.created, later.reason) == (False, "already_active")


def test_supersede_cancels_the_live_job(migrated: Settings) -> None:
    """§6.0: an upgrade replaces the file the running job is cleaning."""
    item = make_items()[0]
    with session_scope() as session:
        first = enqueue(session, media_item_id=item, trigger="webhook")
    claim_next(worker_id="w1", settings=migrated)
    with session_scope() as session:
        second = enqueue(session, media_item_id=item, trigger="webhook", supersede=True)
    assert second.created and second.superseded == (first.job_id,)
    with session_scope() as session:
        old = session.get(Job, first.job_id)
        assert old.state == "cancelled" and old.error == "superseded"
        assert session.get(Job, second.job_id).state == "queued"


def test_a_terminal_job_does_not_block_the_next_one(migrated: Settings) -> None:
    item = make_items()[0]
    with session_scope() as session:
        first = enqueue(session, media_item_id=item, trigger="webhook")
        session.get(Job, first.job_id).state = "done"
        session.flush()
        second = enqueue(session, media_item_id=item, trigger="reprocess")
    assert second.created and second.job_id != first.job_id


def test_enqueue_carries_the_job_parameters(migrated: Settings) -> None:
    item = make_items()[0]
    with session_scope() as session:
        result = enqueue(
            session,
            media_item_id=item,
            trigger="audit",
            stt_mode="audit",
            force=True,
            dry_run=True,
        )
    claim = claim_next(worker_id="w1", settings=migrated)
    assert claim is not None
    assert (claim.job_id, claim.stt_mode, claim.force, claim.dry_run, claim.trigger) == (
        result.job_id,
        "audit",
        True,
        True,
        "audit",
    )


# ---------------------------------------------------------------- cancel/repri


def test_cancel_returns_the_item_to_pending(migrated: Settings) -> None:
    item = make_items()[0]
    with session_scope() as session:
        result = enqueue(session, media_item_id=item, trigger="webhook")
    with session_scope() as session:
        assert cancel(session, result.job_id) is True
        assert session.get(MediaItem, item).status == "pending"
    with session_scope() as session:
        assert cancel(session, result.job_id) is False  # already terminal
    assert claim_next(worker_id="w1", settings=migrated) is None


def test_reprioritize_only_touches_queued_jobs(migrated: Settings) -> None:
    item = make_items()[0]
    add_job(item, id="j", priority=200)
    with session_scope() as session:
        assert reprioritize(session, "j", 10) is True
        assert session.get(Job, "j").priority == 10
    claim_next(worker_id="w1", settings=migrated)
    with session_scope() as session:
        assert reprioritize(session, "j", 999) is False
