"""A claimed `jobs` row becomes a `JobSpec` -- and resume stays honest.

The invalidation test is the point of this file. Worker job dirs are named by the
`jobs` uuid, so unlike the CLI they do *not* change name when the profile changes,
and `build_context` overwrites `job.json` unconditionally. Without the check, a
reprocess after a whitelist edit would resume onto a transcript and a detection set
computed for the old word list, and skip both stages that would have noticed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from vidcleaner.config import Settings
from vidcleaner.db.models import Job, MediaItem, Title, WhitelistEntry
from vidcleaner.db.session import session_scope
from vidcleaner.matching.profile import clear_matcher_cache, ensure_seed_data
from vidcleaner.pipeline.workspace import Workspace
from vidcleaner.worker.claim import claim_next, enqueue
from vidcleaner.worker.spec import plan_job


def seeded_item(*, kind: str = "series", arr_id: int = 42, path: str | None = None) -> int:
    ensure_seed_data()
    with session_scope() as session:
        title = Title(kind=kind, arr_id=arr_id, title="Show", enabled=True, tvdb_id=999)
        session.add(title)
        session.flush()
        item = MediaItem(
            title_id=title.id,
            kind="episode" if kind == "series" else "movie",
            arr_file_id=7,
            season=1 if kind == "series" else None,
            episode=1 if kind == "series" else None,
            path=path or "/media/tv/Show/S01E01.mkv",
            status="pending",
        )
        session.add(item)
        session.flush()
        return item.id


def claimed(item_id: int, settings: Settings, **kwargs) -> str:
    with session_scope() as session:
        job_id = enqueue(session, media_item_id=item_id, trigger="webhook", **kwargs).job_id
    claim_next(worker_id="w1", settings=settings)
    return job_id


def plan(job_id: str, settings: Settings):
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return plan_job(session, job, deploy=settings)


def test_the_spec_describes_the_queued_item(migrated: Settings) -> None:
    item_id = seeded_item()
    job_id = claimed(item_id, migrated)
    result = plan(job_id, migrated)

    assert result.spec.job_id == job_id
    assert result.spec.source_path == "/media/tv/Show/S01E01.mkv"
    assert result.spec.trigger == "webhook"
    assert result.spec.profile_hash.startswith("v1:")
    # render writes /work/<uuid>/out.mkv and swap moves it; --out is the CLI's flag.
    assert result.spec.out_path is None
    assert result.spec.in_place is True
    assert result.ws.root.name == job_id


def test_a_dry_run_never_touches_the_library(migrated: Settings) -> None:
    item_id = seeded_item()
    job_id = claimed(item_id, migrated, dry_run=True)
    assert plan(job_id, migrated).spec.in_place is False


def test_the_target_carries_ids_and_no_credentials(migrated: Settings) -> None:
    item_id = seeded_item()
    result = plan(claimed(item_id, migrated), migrated)
    target = result.spec.target
    assert target is not None
    assert (target.arr_app, target.arr_id, target.arr_file_id) == ("sonarr", 42, 7)
    assert (target.season, target.episode, target.tvdb_id) == (1, 1, 999)
    assert target.media_item_id == item_id
    dumped = result.spec.model_dump_json()
    assert "api_key" not in dumped


def test_a_movie_targets_radarr(migrated: Settings) -> None:
    item_id = seeded_item(kind="movie", arr_id=5, path="/media/movies/Film/Film.mkv")
    target = plan(claimed(item_id, migrated), migrated).spec.target
    assert target is not None and target.arr_app == "radarr"


def test_a_cli_sentinel_title_has_no_arr_app(migrated: Settings) -> None:
    """The M1 sentinel is arr_id=-1; `refresh` must not try to rescan it."""
    item_id = seeded_item(arr_id=-1)
    target = plan(claimed(item_id, migrated), migrated).spec.target
    assert target is not None and target.arr_app is None


def test_the_profile_hash_is_item_specific(migrated: Settings) -> None:
    """An item whitelist must change the hash, or "whitelist then reprocess" would
    be short-circuited to `already_clean` by the tag already in the file."""
    item_id = seeded_item()
    before = plan(claimed(item_id, migrated), migrated).spec.profile_hash

    with session_scope() as session:
        session.add(
            WhitelistEntry(scope="item", scope_id=item_id, canonical_word="god", context_text=None)
        )
    clear_matcher_cache()

    after = plan(_only_job(item_id), migrated).spec.profile_hash
    assert after != before


def _only_job(item_id: int) -> str:
    with session_scope() as session:
        return session.scalars(select(Job.id).where(Job.media_item_id == item_id)).one()


# --------------------------------------------------------------- invalidation


def test_a_matching_work_dir_resumes(migrated: Settings) -> None:
    item_id = seeded_item()
    job_id = claimed(item_id, migrated)
    first = plan(job_id, migrated)
    first.spec.write(first.ws.job_spec)
    first.ws.mark_done("probe")
    first.ws.mark_done("extract")

    again = plan(job_id, migrated)
    assert again.invalidated is None
    assert again.completed == ("probe", "extract")


def test_a_changed_profile_discards_the_work_dir(migrated: Settings) -> None:
    item_id = seeded_item()
    job_id = claimed(item_id, migrated)
    first = plan(job_id, migrated)
    first.spec.write(first.ws.job_spec)
    for stage in ("probe", "extract", "subtitles", "transcribe", "detect"):
        first.ws.mark_done(stage)

    with session_scope() as session:
        session.add(
            WhitelistEntry(scope="item", scope_id=item_id, canonical_word="shit", context_text=None)
        )
    clear_matcher_cache()

    again = plan(job_id, migrated)
    assert again.invalidated is not None and "profile_hash" in again.invalidated
    assert again.completed == ()


def test_a_changed_stt_mode_discards_the_work_dir(migrated: Settings) -> None:
    """The same defect M2 step 1 found for --stt-mode, by a different route."""
    item_id = seeded_item()
    job_id = claimed(item_id, migrated)
    first = plan(job_id, migrated)
    first.spec.write(first.ws.job_spec)
    first.ws.mark_done("transcribe")

    with session_scope() as session:
        session.get(Job, job_id).stt_mode = "full"

    again = plan(job_id, migrated)
    assert again.invalidated is not None and "stt_mode" in again.invalidated
    assert again.completed == ()


def test_an_unreadable_job_spec_discards_the_work_dir(migrated: Settings) -> None:
    item_id = seeded_item()
    job_id = claimed(item_id, migrated)
    ws = Workspace.for_job(job_id, migrated).ensure()
    ws.job_spec.write_text("{ not json")
    ws.mark_done("probe")

    again = plan(job_id, migrated)
    assert again.invalidated is not None
    assert again.completed == ()


def test_a_fresh_work_dir_is_not_reported_as_invalidated(migrated: Settings) -> None:
    item_id = seeded_item()
    result = plan(claimed(item_id, migrated), migrated)
    assert result.invalidated is None and result.completed == ()
    assert result.ws.root.is_dir()


def test_forcing_a_job_does_not_by_itself_invalidate(migrated: Settings) -> None:
    """`force` changes what we do next, not what the existing artifacts mean --
    `run_stage` already honours `spec.force` by ignoring the markers."""
    item_id = seeded_item()
    job_id = claimed(item_id, migrated)
    first = plan(job_id, migrated)
    first.spec.write(first.ws.job_spec)
    first.ws.mark_done("probe")

    with session_scope() as session:
        session.get(Job, job_id).force = True

    again = plan(job_id, migrated)
    assert again.invalidated is None
    assert again.spec.force is True


def test_a_job_whose_item_vanished_raises(migrated: Settings) -> None:
    """The item can be deleted between enqueue and claim (an arr file delete)."""
    ensure_seed_data()
    # Not added to the session: the foreign key would reject it, which is the point.
    orphan = Job(id="orphan", media_item_id=99999, trigger="manual", state="probing")
    with session_scope() as session, pytest.raises(ValueError, match="gone"):
        plan_job(session, orphan, deploy=migrated)
