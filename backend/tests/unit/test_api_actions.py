"""What the M4 screens' buttons do (PLAN.md §9.1-§9.4).

The queue policy itself is `tests/unit/test_claim.py`'s subject; these pin the
translation from a button to a queue row -- which trigger, which priority, and whether
it forces past §4's ``already_clean`` tag.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library import add_detections, add_job, make_movie, make_series
from vidcleaner.db.models import Job, MediaItem, WhitelistEntry
from vidcleaner.db.session import session_scope


def jobs_of(item_id: int) -> list[Job]:
    with session_scope() as session:
        return list(
            session.scalars(
                select(Job).where(Job.media_item_id == item_id).order_by(Job.created_at)
            ).all()
        )


# ---------------------------------------------------------------- the toggle


def test_enabling_a_title_backfills_its_files(client: TestClient) -> None:
    """§2: marking a title enqueues its existing files, now -- not in an hour."""
    title_id, episodes = make_series(episodes=3, enabled=False)
    body = client.patch(f"/api/library/titles/{title_id}", json={"enabled": True}).json()

    assert body["enabled"] is True
    assert len(body["queued"]) == 3
    assert {j.trigger for j in jobs_of(episodes[0])} == {"backfill"}


def test_disabling_a_title_queues_nothing(client: TestClient) -> None:
    title_id, episodes = make_series(episodes=2, enabled=True)
    body = client.patch(f"/api/library/titles/{title_id}", json={"enabled": False}).json()
    assert body["enabled"] is False and body["queued"] == []
    assert jobs_of(episodes[0]) == []


def test_re_enabling_an_enabled_title_does_not_double_queue(client: TestClient) -> None:
    title_id, _ = make_series(episodes=2, enabled=True)
    first = client.patch(f"/api/library/titles/{title_id}", json={"enabled": True}).json()
    assert first["queued"] == [], "it was already on; nothing changed"


def test_a_clean_file_is_not_backfilled_again(client: TestClient) -> None:
    """§8's catch-up gate: clean *for the profile hash it was cleaned under*."""
    import json

    from vidcleaner.matching.profile import matcher_for

    title_id, episodes = make_series(episodes=2, enabled=False)
    with session_scope() as session:
        current = matcher_for(session, title_id=title_id, item_id=episodes[0]).profile_hash
    job_id = add_job(
        episodes[0],
        state="done",
        is_last=True,
        profile_snapshot_json=json.dumps({"profile_hash": current}),
    )
    with session_scope() as session:
        session.get(MediaItem, episodes[0]).status = "clean"

    body = client.patch(f"/api/library/titles/{title_id}", json={"enabled": True}).json()
    assert len(body["queued"]) == 1, "only the pending episode"
    assert job_id not in body["queued"]


def test_a_profile_can_be_set_and_cleared(client: TestClient) -> None:
    from vidcleaner.db.models import Profile

    title_id, _ = make_series(episodes=1)
    with session_scope() as session:
        profile = Profile(name="Mild only", categories_json='["mild"]')
        session.add(profile)
        session.flush()
        profile_id = profile.id

    set_ = client.patch(f"/api/library/titles/{title_id}", json={"profile_id": profile_id}).json()
    assert set_["profile_id"] == profile_id

    cleared = client.patch(f"/api/library/titles/{title_id}", json={"clear_profile": True}).json()
    assert cleared["profile_id"] is None


def test_an_unknown_profile_is_rejected(client: TestClient) -> None:
    title_id, _ = make_series(episodes=1)
    assert (
        client.patch(f"/api/library/titles/{title_id}", json={"profile_id": 42}).status_code == 422
    )


# --------------------------------------------------------------- item actions


@pytest.mark.parametrize(
    ("action", "trigger", "force", "dry_run"),
    [
        ("process", "manual", False, False),
        ("reprocess", "reprocess", True, False),
        ("dry_run", "manual", True, True),
    ],
)
def test_each_button_queues_the_right_kind_of_job(
    client: TestClient, action: str, trigger: str, force: bool, dry_run: bool
) -> None:
    _, item_id = make_movie()
    body = client.post(f"/api/items/{item_id}/actions", json={"action": action}).json()

    assert len(body["queued"]) == 1
    job = jobs_of(item_id)[0]
    assert (job.trigger, job.force, job.dry_run) == (trigger, force, dry_run)


def test_reprocess_forces_past_the_already_clean_tag(client: TestClient) -> None:
    """Without `force`, §4's idempotency check answers instead and the button
    appears to do nothing -- the exact confusion it exists to resolve."""
    _, item_id = make_movie(status="clean")
    client.post(f"/api/items/{item_id}/actions", json={"action": "reprocess"})
    assert jobs_of(item_id)[0].force is True


def test_a_second_press_is_reported_not_duplicated(client: TestClient) -> None:
    _, item_id = make_movie()
    client.post(f"/api/items/{item_id}/actions", json={"action": "process"})
    body = client.post(f"/api/items/{item_id}/actions", json={"action": "process"}).json()

    assert body["queued"] == []
    assert sum(body["skipped"].values()) == 1
    assert len(jobs_of(item_id)) == 1


def test_a_title_action_covers_every_episode(client: TestClient) -> None:
    title_id, episodes = make_series(episodes=3)
    body = client.post(f"/api/library/titles/{title_id}/actions", json={"action": "process"}).json()
    assert body["considered"] == 3 and len(body["queued"]) == 3
    assert all(jobs_of(item_id) for item_id in episodes)


def test_an_unknown_action_is_rejected(client: TestClient) -> None:
    _, item_id = make_movie()
    assert (
        client.post(f"/api/items/{item_id}/actions", json={"action": "delete"}).status_code == 422
    )


# -------------------------------------------------------------------- restore


def test_restore_reports_items_with_no_backup(client: TestClient) -> None:
    _, item_id = make_movie()
    body = client.post(f"/api/items/{item_id}/actions", json={"action": "restore"}).json()
    assert body["restored"] == [] and body["skipped"] == {"no_backup": 1}


def test_restore_puts_the_original_back(client: TestClient, tmp_path) -> None:
    """The rename itself is `test_swap_restore.py`'s subject; this is the wiring."""
    original = tmp_path / "Film.mkv"
    original.write_bytes(b"cleaned")
    backup = tmp_path / "backup.mkv"
    backup.write_bytes(b"original")

    _, item_id = make_movie(status="clean")
    with session_scope() as session:
        session.get(MediaItem, item_id).path = str(original)
    from vidcleaner.db.models import Backup

    with session_scope() as session:
        session.add(
            Backup(
                media_item_id=item_id,
                original_path=str(original),
                backup_path=str(backup),
                size=8,
                state="kept",
            )
        )

    body = client.post(f"/api/items/{item_id}/actions", json={"action": "restore"}).json()
    assert body["restored"] == [item_id]
    assert original.read_bytes() == b"original"


# ------------------------------------------------------------------ whitelist


def test_whitelisting_a_word_queues_a_reprocess(client: TestClient) -> None:
    title_id, item_id = make_movie()
    job_id = add_job(item_id, state="done", is_last=True)
    add_detections(job_id, item_id, "bass")

    body = client.post(
        f"/api/items/{item_id}/whitelist",
        json={"canonical_word": "Bass", "scope": "item"},
    ).json()

    assert body["created"] is True
    assert body["canonical_word"] == "bass", "canonicals are lower case"
    assert body["scope_id"] == item_id
    assert body["job_id"] is not None
    assert jobs_of(item_id)[-1].force is True


def test_the_scope_decides_what_the_entry_points_at(client: TestClient) -> None:
    title_id, item_id = make_movie()
    for scope, expected in (("item", item_id), ("title", title_id), ("global", None)):
        body = client.post(
            f"/api/items/{item_id}/whitelist",
            json={"canonical_word": f"word-{scope}", "scope": scope, "reprocess": False},
        ).json()
        assert body["scope_id"] == expected


def test_whitelisting_the_same_word_twice_is_idempotent(client: TestClient) -> None:
    _, item_id = make_movie()
    first = client.post(
        f"/api/items/{item_id}/whitelist",
        json={"canonical_word": "bass", "reprocess": False},
    ).json()
    second = client.post(
        f"/api/items/{item_id}/whitelist",
        json={"canonical_word": "bass", "reprocess": False},
    ).json()

    assert second["created"] is False and second["id"] == first["id"]


def test_a_whitelist_entry_can_be_removed(client: TestClient) -> None:
    _, item_id = make_movie()
    entry = client.post(
        f"/api/items/{item_id}/whitelist",
        json={"canonical_word": "bass", "reprocess": False},
    ).json()

    assert client.delete(f"/api/whitelist/{entry['id']}").status_code == 204
    with session_scope() as session:
        assert session.get(WhitelistEntry, entry["id"]) is None
    assert client.delete(f"/api/whitelist/{entry['id']}").status_code == 404


def test_whitelisting_without_reprocess_queues_nothing(client: TestClient) -> None:
    _, item_id = make_movie()
    body = client.post(
        f"/api/items/{item_id}/whitelist",
        json={"canonical_word": "bass", "reprocess": False},
    ).json()
    assert body["job_id"] is None and jobs_of(item_id) == []


# ----------------------------------------------------------------- queue rows


def test_a_queued_job_can_be_cancelled(client: TestClient) -> None:
    _, item_id = make_movie()
    job_id = add_job(item_id, state="queued")

    assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": True}
    with session_scope() as session:
        assert session.get(Job, job_id).state == "cancelled"


def test_cancelling_a_finished_job_changes_nothing(client: TestClient) -> None:
    _, item_id = make_movie()
    job_id = add_job(item_id, state="done")
    assert client.post(f"/api/jobs/{job_id}/cancel").json() == {"cancelled": False}


def test_a_queued_job_can_be_reordered(client: TestClient) -> None:
    _, item_id = make_movie()
    job_id = add_job(item_id, state="queued", priority=200)

    assert client.patch(f"/api/jobs/{job_id}", json={"priority": 10}).json() == {"updated": True}
    with session_scope() as session:
        assert session.get(Job, job_id).priority == 10


def test_a_running_job_cannot_be_reordered(client: TestClient) -> None:
    """Its place in the queue has already been spent."""
    _, item_id = make_movie()
    job_id = add_job(item_id, state="rendering")
    assert client.patch(f"/api/jobs/{job_id}", json={"priority": 10}).json() == {"updated": False}


def test_retry_creates_a_new_job_rather_than_reviving_the_old_one(client: TestClient) -> None:
    _, item_id = make_movie()
    failed = add_job(item_id, state="failed", is_last=True)

    body = client.post(f"/api/jobs/{failed}/retry").json()
    assert len(body["queued"]) == 1 and body["queued"][0] != failed
    with session_scope() as session:
        assert session.get(Job, failed).state == "failed", "the record of what ran stands"


def test_actions_on_unknown_rows_are_404s(client: TestClient) -> None:
    assert client.post("/api/items/999/actions", json={"action": "process"}).status_code == 404
    assert (
        client.post("/api/library/titles/999/actions", json={"action": "process"}).status_code
        == 404
    )
    assert client.patch("/api/library/titles/999", json={"enabled": True}).status_code == 404
    assert client.post("/api/jobs/nope/cancel").status_code == 404
    assert client.post("/api/jobs/nope/retry").status_code == 404
    assert client.post("/api/items/999/whitelist", json={"canonical_word": "x"}).status_code == 404
