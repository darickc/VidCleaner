"""§13's retention: the one place in the project that deletes a user's originals.

Until M5 `backups.purge_after` had three writers and no readers -- the column was
filled in on every swap, the number was editable in Settings, and nothing ever deleted
anything. These tests pin both halves: that it does reclaim the space, and that the
four refusals hold, because the files at stake are the entire basis of "every change is
reversible".
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library import add_backup, add_job, make_movie, make_series
from vidcleaner.config import Settings, get_settings
from vidcleaner.db.models import Backup, MediaItem
from vidcleaner.db.session import session_scope, utcnow
from vidcleaner.worker.purge import purge_backups


def backup_file(settings: Settings, name: str = "S01E01.mkv", size: int = 2048):
    """A real file inside `backups_dir`, which is what the guard requires."""
    folder = settings.backups_dir / "tv" / "Show"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"o" * size)
    return path


def held(item_id: int, path, *, state: str = "kept", days_ago: float | None = 1.0) -> int:
    backup_id = add_backup(item_id, state=state, backup_path=str(path))
    with session_scope() as session:
        row = session.get(Backup, backup_id)
        row.size = path.stat().st_size
        row.purge_after = None if days_ago is None else utcnow() - timedelta(days=days_ago)
    return backup_id


def state_of(backup_id: int) -> str:
    with session_scope() as session:
        return session.get(Backup, backup_id).state


def run_purge(settings: Settings, **kw):
    with session_scope() as session:
        return purge_backups(session, settings, **kw)


# ------------------------------------------------------------------ the happy path


def test_an_expired_backup_is_deleted_and_marked(migrated: Settings) -> None:
    _title, item_id = make_movie()
    path = backup_file(migrated)
    backup_id = held(item_id, path)

    report = run_purge(migrated)

    assert report.purged == 1
    assert report.freed_bytes == 2048
    assert not path.exists()
    assert state_of(backup_id) == "purged"


def test_a_backup_still_in_date_is_left_alone(migrated: Settings) -> None:
    _title, item_id = make_movie()
    path = backup_file(migrated)
    backup_id = held(item_id, path, days_ago=-5.0)  # expires in five days

    assert run_purge(migrated).purged == 0
    assert path.exists()
    assert state_of(backup_id) == "kept"


def test_a_null_purge_after_is_never_purged(migrated: Settings) -> None:
    """That is how `backup_retention_days = 0` expresses "keep forever", and it must
    not be reinterpretable as "expired long ago"."""
    _title, item_id = make_movie()
    path = backup_file(migrated)
    backup_id = held(item_id, path, days_ago=None)

    assert run_purge(migrated).purged == 0
    assert path.exists()
    assert state_of(backup_id) == "kept"


def test_a_restored_backup_is_not_purgeable(migrated: Settings) -> None:
    """Its file went back into the library; the row is history, not storage."""
    _title, item_id = make_movie()
    path = backup_file(migrated)
    backup_id = held(item_id, path, state="restored")

    assert run_purge(migrated).purged == 0
    assert path.exists()
    assert state_of(backup_id) == "restored"


def test_a_missing_file_is_reconciled_not_an_error(migrated: Settings) -> None:
    """`reconcile_backups` already models this: a human deleted it, or a previous run
    got as far as the unlink and not the commit."""
    _title, item_id = make_movie()
    path = backup_file(migrated)
    backup_id = held(item_id, path)
    path.unlink()

    report = run_purge(migrated)
    assert report.purged == 0
    assert report.missing == 1
    assert state_of(backup_id) == "purged"


# ---------------------------------------------------------------- the refusals


def test_a_path_outside_the_backups_dir_is_refused(migrated: Settings) -> None:
    """A row is data. A `backup_path` pointing into the library -- however it got
    there -- must cost nothing."""
    _title, item_id = make_movie()
    victim = migrated.media_dir / "movies" / "Film.mkv"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"precious")
    backup_id = held(item_id, victim)

    report = run_purge(migrated)
    assert report.purged == 0
    assert report.refused and "outside" in report.refused[0]
    assert victim.read_bytes() == b"precious"
    assert state_of(backup_id) == "kept"


def test_a_symlink_out_of_the_backups_dir_is_refused(migrated: Settings) -> None:
    """`resolve()` is what makes this fail rather than following the link."""
    _title, item_id = make_movie()
    victim = migrated.media_dir / "movies" / "Film.mkv"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"precious")
    link = migrated.backups_dir / "sneaky.mkv"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(victim)
    held(item_id, link)

    report = run_purge(migrated)
    assert report.purged == 0
    assert report.refused
    assert victim.read_bytes() == b"precious"


# --------------------------------------------------------------------- scopes


def test_the_orphaned_scope_ignores_the_clock(migrated: Settings) -> None:
    """An upgrade's leftover cannot be restored by anything, so §9.6 offers it for
    reclaim now rather than in thirty days."""
    _title, item_id = make_movie()
    path = backup_file(migrated)
    backup_id = held(item_id, path, state="orphaned", days_ago=-30.0)

    assert run_purge(migrated).purged == 0, "not expired yet"
    assert run_purge(migrated, scope="orphaned").purged == 1
    assert state_of(backup_id) == "purged"


def test_the_expired_scope_covers_orphans_that_are_due(migrated: Settings) -> None:
    _title, item_id = make_movie()
    path = backup_file(migrated)
    held(item_id, path, state="orphaned", days_ago=1.0)
    assert run_purge(migrated).purged == 1


def test_the_last_original_of_a_clean_file_is_still_purged_when_due(
    migrated: Settings,
) -> None:
    """Its own clock ran out, so retention means what it says -- but the log records
    the moment "restore original" stops being possible for that episode."""
    _title_id, episodes = make_series(episodes=1)
    item_id = episodes[0]
    with session_scope() as session:
        session.get(MediaItem, item_id).status = "clean"
    path = backup_file(migrated)
    backup_id = held(item_id, path)

    assert run_purge(migrated).purged == 1
    assert state_of(backup_id) == "purged"


# ------------------------------------------------------------------- the API


def test_the_api_summarises_what_is_held(client: TestClient, settings: Settings) -> None:
    _title, item_id = make_movie()
    path = backup_file(get_settings())
    held(item_id, path)

    body = client.get("/api/backups").json()
    assert body["summary"]["total"] == 1
    assert body["summary"]["total_bytes"] == 2048
    assert body["summary"]["expired"] == 1
    assert body["summary"]["expired_bytes"] == 2048
    assert body["summary"]["retention_days"] == 30
    assert body["summary"]["keeps_forever"] is False
    row = body["backups"][0]
    assert row["expired"] is True
    assert row["exists"] is True
    assert row["label"] == "Film (1999)"


def test_the_api_reports_keep_forever(client: TestClient) -> None:
    client.patch("/api/settings", json={"backup_retention_days": 0})
    body = client.get("/api/backups").json()
    assert body["summary"]["keeps_forever"] is True


def test_the_purge_button_reclaims_the_space(client: TestClient) -> None:
    _title, item_id = make_movie()
    path = backup_file(get_settings())
    held(item_id, path)

    result = client.post("/api/backups/purge", json={"scope": "expired"}).json()
    assert result["purged"] == 1
    assert result["freed_bytes"] == 2048
    assert not path.exists()
    assert client.get("/api/backups").json()["summary"]["expired"] == 0


def test_the_purge_button_reports_a_refusal_rather_than_failing(client: TestClient) -> None:
    settings = get_settings()
    _title, item_id = make_movie()
    victim = settings.media_dir / "movies" / "Film.mkv"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(b"precious")
    held(item_id, victim)

    result = client.post("/api/backups/purge", json={"scope": "expired"}).json()
    assert result["purged"] == 0
    assert result["warnings"]
    assert victim.exists()


def test_backups_can_be_filtered_by_state(client: TestClient) -> None:
    _title, item_id = make_movie()
    path = backup_file(get_settings())
    held(item_id, path, state="orphaned")
    assert len(client.get("/api/backups?state=orphaned").json()["backups"]) == 1
    assert client.get("/api/backups?state=kept").json()["backups"] == []


def test_a_backup_whose_file_vanished_is_reported_as_missing(client: TestClient) -> None:
    """So the page can say "gone" instead of offering to reclaim nothing."""
    _title, item_id = make_movie()
    path = backup_file(get_settings())
    held(item_id, path)
    path.unlink()
    assert client.get("/api/backups").json()["backups"][0]["exists"] is False


def test_a_job_id_survives_on_the_row(client: TestClient) -> None:
    """`backups.job_id` has no FK precisely so it outlives the pruned job."""
    _title, item_id = make_movie()
    job_id = add_job(item_id, state="done")
    path = backup_file(get_settings())
    add_backup(item_id, job_id=job_id, backup_path=str(path))
    with session_scope() as session:
        assert session.scalars(select(Backup)).one().job_id == job_id


# ----------------------------------------------------- the scheduler's task


def test_the_scheduler_reconciles_before_it_purges(migrated: Settings) -> None:
    """Order matters. A crash between the rename and the commit leaves a file in
    `/backups` that no row knows about, and the purge deliberately never walks the
    directory -- so without the reconcile first that file would sit there forever.
    """
    from vidcleaner.worker.scheduler import Scheduler

    _title, item_id = make_movie()
    tracked = backup_file(migrated, "tracked.mkv")
    held(item_id, tracked)
    stray = backup_file(migrated, "stray.mkv")  # on disk, no row

    ran = Scheduler(migrated).tick()
    assert "retention" in ran

    with session_scope() as session:
        rows = {b.backup_path: b.state for b in session.scalars(select(Backup)).all()}
    assert rows[str(tracked)] == "purged", "the expired one went"
    assert not tracked.exists()
    assert str(stray) in rows, "and the stray was adopted rather than left unknown"


def test_turning_retention_off_stops_the_scheduled_purge(migrated: Settings) -> None:
    """Rows written while retention was on still carry a date, so the switch has to be
    re-checked at purge time or turning it off would not actually stop the deletions."""
    from vidcleaner.settings_store import save_settings
    from vidcleaner.worker.scheduler import Scheduler

    _title, item_id = make_movie()
    path = backup_file(migrated)
    held(item_id, path)
    with session_scope() as session:
        save_settings(session, {"backup_retention_days": 0})

    Scheduler(migrated).tick()
    assert path.exists(), "keep forever means keep forever"
