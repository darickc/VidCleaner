"""§9.7's Backups page: what is being held, and the two ways to act on one row.

The summary has existed since M5; this tier is about the *list*, because an orphan
that is only a count cannot be checked before it is deleted.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from tests.support.library import add_backup, make_movie
from vidcleaner.config import Settings
from vidcleaner.db.models import Backup
from vidcleaner.db.session import session_scope, utcnow


def backup_file(settings: Settings, relative: str, size: int = 2048) -> Path:
    path = settings.backups_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"o" * size)
    return path


def held(item_id: int, path: Path, *, state: str = "kept", days_ago: float | None = 1.0):
    backup_id = add_backup(item_id, state=state, backup_path=str(path))
    with session_scope() as session:
        row = session.get(Backup, backup_id)
        row.size = path.stat().st_size
        row.purge_after = None if days_ago is None else utcnow() - timedelta(days=days_ago)
    return backup_id


def test_a_row_is_named_by_its_path_under_the_backups_directory(
    client: TestClient, migrated: Settings
) -> None:
    """The backups tree mirrors the library tree, so this is the readable name --
    and the only one an adopted orphan has."""
    _title, item_id = make_movie()
    held(item_id, backup_file(migrated, "movies/Film (1999)/Film.mkv"))

    row = client.get("/api/backups").json()["backups"][0]

    assert row["rel_path"] == "movies/Film (1999)/Film.mkv"
    assert row["identified"] is True
    assert row["exists"] is True


def test_an_adopted_orphan_offers_no_item_link(client: TestClient, migrated: Settings) -> None:
    """It hangs off the `<orphaned backups>` sentinel: there is no episode page to go
    to, and nothing to restore it into."""
    from vidcleaner.pipeline.persist import reconcile_backups

    backup_file(migrated, "tv/Show/S01E01.mkv")
    with session_scope() as session:
        reconcile_backups(session, migrated.backups_dir)

    rows = client.get("/api/backups?state=orphaned").json()["backups"]

    assert len(rows) == 1
    assert rows[0]["identified"] is False
    assert rows[0]["rel_path"] == "tv/Show/S01E01.mkv"


def test_largest_first_answers_what_is_filling_the_share(
    client: TestClient, migrated: Settings
) -> None:
    _title, item_id = make_movie()
    held(item_id, backup_file(migrated, "movies/Small.mkv", size=10))
    held(item_id, backup_file(migrated, "movies/Huge.mkv", size=9000))

    names = [r["rel_path"] for r in client.get("/api/backups?sort=largest").json()["backups"]]

    assert names == ["movies/Huge.mkv", "movies/Small.mkv"]


def test_expired_only_shows_exactly_what_the_purge_button_would_take(
    client: TestClient, migrated: Settings
) -> None:
    _title, item_id = make_movie()
    held(item_id, backup_file(migrated, "movies/Gone.mkv"), days_ago=1.0)
    held(item_id, backup_file(migrated, "movies/Safe.mkv"), days_ago=-30.0)

    body = client.get("/api/backups?expired_only=true").json()

    assert [r["rel_path"] for r in body["backups"]] == ["movies/Gone.mkv"]
    assert body["summary"]["expired"] == 1


def test_one_row_can_be_purged_on_its_own(client: TestClient, migrated: Settings) -> None:
    _title, item_id = make_movie()
    doomed = backup_file(migrated, "movies/Huge.mkv", size=4096)
    kept = backup_file(migrated, "movies/Keep.mkv")
    backup_id = held(item_id, doomed)
    held(item_id, kept)

    body = client.delete(f"/api/backups/{backup_id}").json()

    assert body["purged"] == 1
    assert body["freed_bytes"] == 4096
    assert not doomed.exists()
    assert kept.exists()
    with session_scope() as session:
        assert session.get(Backup, backup_id).state == "purged"


def test_a_restored_row_is_refused(client: TestClient, migrated: Settings) -> None:
    """Its file went back into the library; deleting by that path would take the
    user's actual media. `purge_backups` has always refused it and still does."""
    _title, item_id = make_movie()
    path = backup_file(migrated, "movies/Film.mkv")
    backup_id = held(item_id, path, state="restored")

    response = client.delete(f"/api/backups/{backup_id}")

    assert response.status_code == 409
    assert path.exists()


def test_purging_an_unknown_row_is_a_404(client: TestClient, migrated: Settings) -> None:
    assert client.delete("/api/backups/999").status_code == 404


def test_reconcile_picks_up_a_file_no_row_knows_about(
    client: TestClient, migrated: Settings
) -> None:
    """The page's whole purpose is looking at orphans before deleting them, so it
    must not show an hour-old picture."""
    backup_file(migrated, "tv/Show/S01E02.mkv")

    body = client.post("/api/backups/reconcile").json()

    assert body["adopted"] == 1
    assert body["skipped"] is False
    assert client.get("/api/backups").json()["summary"]["orphaned"] == 1
