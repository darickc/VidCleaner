"""The webhook receivers (PLAN.md §6 step 0, §8, §12's contract tier).

Three properties, all with §3 reasons: always 200 except 401/400 (Sonarr disables a
notification that keeps failing); dispatch is pure database, so "respond immediately"
is true without a BackgroundTask; and `Test` is handled before any title resolution,
because a Test payload carries dummy ids.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from vidcleaner.db.models import Backup, Job, MediaItem, PathMapping, Title, WebhookEvent
from vidcleaner.db.session import session_scope
from vidcleaner.settings_store import load_settings

E01 = "/tv/Show/Season 01/Show - S01E01.mkv"
LOCAL_E01 = "/media/tv/Show/Season 01/Show - S01E01.mkv"


@pytest.fixture
def token(client: TestClient) -> str:
    """Generated at seed time, so the receiver can always require it."""
    with session_scope() as session:
        value = load_settings(session).webhook_token
    assert value, "ensure_seed_data must mint a token"
    return value


@pytest.fixture
def mapped(client: TestClient) -> None:
    client.put(
        "/api/path-mappings",
        json=[{"app": "sonarr", "from_prefix": "/tv", "to_prefix": "/media/tv"}],
    )


@pytest.fixture
def series(client: TestClient) -> int:
    with session_scope() as session:
        title = Title(kind="series", arr_id=42, title="Show", enabled=True)
        session.add(title)
        session.flush()
        return title.id


def download(
    *, upgrade: bool = False, path: str = E01, file_id: int = 501, deleted: list[str] | None = None
) -> dict[str, Any]:
    return {
        "eventType": "Download",
        "series": {"id": 42, "title": "Show", "path": "/tv/Show", "tvdbId": 1},
        "episodes": [{"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "Pilot"}],
        "episodeFile": {"id": file_id, "path": path, "relativePath": "x.mkv", "size": 1000},
        "isUpgrade": upgrade,
        "deletedFiles": [{"path": p} for p in (deleted or [])],
    }


def post(client: TestClient, token: str, payload: dict[str, Any], *, app: str = "sonarr"):
    return client.post(f"/api/webhooks/{app}", json=payload, headers={"X-VidCleaner-Token": token})


def jobs() -> list[Job]:
    with session_scope() as session:
        rows = session.scalars(select(Job)).all()
        for row in rows:
            session.expunge(row)
        return rows


def events() -> list[WebhookEvent]:
    with session_scope() as session:
        rows = session.scalars(select(WebhookEvent).order_by(WebhookEvent.id)).all()
        for row in rows:
            session.expunge(row)
        return rows


# --------------------------------------------------------------------- auth


def test_a_missing_token_is_rejected_and_stores_nothing(client: TestClient) -> None:
    """Writing unauthenticated bodies would be a denial-of-service vector."""
    assert client.post("/api/webhooks/sonarr", json={"eventType": "Test"}).status_code == 401
    assert events() == []


def test_a_wrong_token_is_rejected(client: TestClient, token: str) -> None:
    response = client.post(
        "/api/webhooks/sonarr",
        json={"eventType": "Test"},
        headers={"X-VidCleaner-Token": token + "x"},
    )
    assert response.status_code == 401
    assert events() == []


def test_the_token_is_generated_so_the_check_is_never_optional(token: str) -> None:
    assert len(token) >= 32


def test_the_setup_page_gives_the_url_header_and_token(client: TestClient, token: str) -> None:
    body = client.get("/api/webhooks/setup?app=radarr").json()
    assert body["url"].endswith("/api/webhooks/radarr")
    assert body["header_name"] == "X-VidCleaner-Token"
    assert body["token"] == token


# ----------------------------------------------------------------- protocol


def test_malformed_json_is_a_400(client: TestClient, token: str) -> None:
    response = client.post(
        "/api/webhooks/sonarr",
        content=b"{not json",
        headers={"X-VidCleaner-Token": token, "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_an_unknown_event_is_acknowledged(client: TestClient, token: str) -> None:
    """Sonarr disables a notification after repeated failures."""
    response = post(client, token, {"eventType": "SomethingNew"})
    assert response.status_code == 200
    assert events()[0].note == "unhandled:SomethingNew"
    assert events()[0].handled is True


def test_the_raw_body_is_stored_verbatim(client: TestClient, token: str) -> None:
    """§6.0: "store raw webhook". The value of the row is that it is what arrived."""
    payload = {"eventType": "Grab", "series": {"id": 42}, "unknownField": [1, 2, 3]}
    post(client, token, payload)
    import json

    assert json.loads(events()[0].payload_json)["unknownField"] == [1, 2, 3]


def test_a_health_event_is_stored_only(client: TestClient, token: str) -> None:
    post(client, token, {"eventType": "Health", "level": "warning"})
    assert events()[0].note == "stored:Health"
    assert jobs() == []


# --------------------------------------------------------------------- Test


def test_a_test_payload_never_enqueues_anything(client: TestClient, token: str) -> None:
    """Test payloads carry dummy ids (`series.id = 1`), so the branch must come
    before any title resolution -- otherwise a Test click would enqueue a real job
    against a fake path on any install whose series 1 exists."""
    with session_scope() as session:
        title = Title(kind="series", arr_id=1, title="Real series one", enabled=True)
        session.add(title)
        session.flush()
        session.add(
            MediaItem(title_id=title.id, kind="episode", season=1, episode=1, path="/media/x.mkv")
        )

    response = post(
        client,
        token,
        {"eventType": "Test", "series": {"id": 1, "title": "Test Title"}, "episodes": []},
    )
    assert response.status_code == 200
    assert events()[0].note == "test"
    assert jobs() == []


# ------------------------------------------------------------------ Download


def test_a_download_for_an_enabled_series_queues_a_job(
    client: TestClient, token: str, mapped, series
) -> None:
    response = post(client, token, download())
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] is not None

    queued = jobs()
    assert len(queued) == 1
    assert (queued[0].trigger, queued[0].priority, queued[0].state) == ("webhook", 100, "queued")
    with session_scope() as session:
        item = session.get(MediaItem, queued[0].media_item_id)
        assert item.path == LOCAL_E01, "the path is mapped before it reaches the database"
        assert item.arr_file_id == 501
        assert item.episode_title == "Pilot"


def test_a_download_for_a_disabled_title_is_recorded_but_not_queued(
    client: TestClient, token: str, mapped
) -> None:
    """§12 asks for exactly this. The row is still upserted, so §9.2's "12/24 clean"
    is right the moment the user enables the title."""
    with session_scope() as session:
        session.add(Title(kind="series", arr_id=42, title="Show", enabled=False))

    assert post(client, token, download()).status_code == 200
    assert events()[0].note == "title_disabled"
    assert jobs() == []
    with session_scope() as session:
        assert session.scalars(select(MediaItem)).one().path == LOCAL_E01


def test_an_unknown_series_is_recorded_for_the_sync_to_adopt(
    client: TestClient, token: str
) -> None:
    """Blocking the receiver on the arr's API to look it up would defeat §6.0's
    "respond 200 immediately"."""
    assert post(client, token, download()).status_code == 200
    assert events()[0].note == "unknown_title"
    assert jobs() == []


def test_a_duplicate_delivery_inside_the_window_reuses_the_job(
    client: TestClient, token: str, mapped, series
) -> None:
    first = post(client, token, download()).json()
    second = post(client, token, download()).json()
    assert second["job_id"] == first["job_id"]
    assert second["note"] == "deduped"
    assert len(jobs()) == 1


def test_a_season_pack_produces_one_job_per_file(
    client: TestClient, token: str, mapped, series
) -> None:
    """§6.0 calls the 60 s window "dedupe by path (season packs)", but a season-pack
    import fires one Download per episode *file*, so every event names a different
    path and the window collapses nothing. This pins that."""
    for episode in range(1, 11):
        payload = download(
            path=f"/tv/Show/Season 01/Show - S01E{episode:02d}.mkv", file_id=500 + episode
        )
        payload["episodes"] = [
            {"id": episode, "seasonNumber": 1, "episodeNumber": episode, "title": f"E{episode}"}
        ]
        assert post(client, token, payload).status_code == 200

    queued = jobs()
    assert len(queued) == 10
    with session_scope() as session:
        items = session.scalars(select(MediaItem)).all()
        assert len(items) == 10
        assert len({i.path for i in items}) == 10


def test_a_multi_episode_file_produces_one_job(
    client: TestClient, token: str, mapped, series
) -> None:
    """One `episodeFile`, several `episodes[]` -- what the window actually protects
    against. §5 cannot represent it, so we key on the lowest episode."""
    payload = download()
    payload["episodes"] = [
        {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "Part One"},
        {"id": 2, "seasonNumber": 1, "episodeNumber": 2, "title": "Part Two"},
    ]
    post(client, token, payload)
    post(client, token, payload)

    assert len(jobs()) == 1
    with session_scope() as session:
        item = session.scalars(select(MediaItem)).one()
        assert (item.season, item.episode) == (1, 1)
        assert item.episode_title == "Part One + Part Two"


def test_an_upgrade_supersedes_the_running_job_and_orphans_the_backup(
    client: TestClient, token: str, mapped, series
) -> None:
    """§6.0: the running job is cleaning a file that no longer exists. §13: the old
    backup belongs to nothing now."""
    first = post(client, token, download()).json()["job_id"]
    with session_scope() as session:
        job = session.get(Job, first)
        job.state = "transcribing"
        job.claimed_by = "w1"
        session.add(
            Backup(
                media_item_id=job.media_item_id,
                original_path=LOCAL_E01,
                backup_path="/backups/old.mkv",
                state="kept",
            )
        )

    body = post(client, token, download(upgrade=True, file_id=777)).json()
    assert "superseded" in body["note"]

    with session_scope() as session:
        old = session.get(Job, first)
        assert old.state == "cancelled" and old.error == "superseded by upgrade"
        assert session.get(Job, body["job_id"]).state == "queued"
        assert session.scalars(select(Backup)).one().state == "orphaned"


def test_an_upgrade_with_nothing_running_simply_queues(
    client: TestClient, token: str, mapped, series
) -> None:
    body = post(client, token, download(upgrade=True)).json()
    assert body["job_id"] is not None
    assert len(jobs()) == 1


# ------------------------------------------------------- delete and rename


def test_a_file_delete_marks_the_item_stale(client: TestClient, token: str, mapped, series) -> None:
    """§6.0 says `pending`; the file is gone, so `stale` is the right word -- and on
    `deleteReason: upgrade` this fires *before* the replacement Download, where
    `stale -> queued` is correct and `pending` would leave a phantom to-do."""
    post(client, token, download())
    with session_scope() as session:
        item = session.scalars(select(MediaItem)).one()
        session.add(
            Backup(
                media_item_id=item.id,
                original_path=LOCAL_E01,
                backup_path="/backups/old.mkv",
                state="kept",
            )
        )

    response = post(
        client,
        token,
        {
            "eventType": "EpisodeFileDelete",
            "series": {"id": 42},
            "episodeFile": {"id": 501, "path": E01},
            "deleteReason": "upgrade",
        },
    )
    assert response.status_code == 200
    with session_scope() as session:
        assert session.scalars(select(MediaItem)).one().status == "stale"
        assert session.scalars(select(Backup)).one().state == "orphaned"


def test_a_series_delete_disables_the_title(client: TestClient, token: str, series) -> None:
    post(client, token, {"eventType": "SeriesDelete", "series": {"id": 42}})
    with session_scope() as session:
        assert session.get(Title, series).enabled is False


def test_series_add_creates_the_title_disabled(client: TestClient, token: str) -> None:
    post(
        client,
        token,
        {"eventType": "SeriesAdd", "series": {"id": 99, "title": "New", "tvdbId": 5}},
    )
    with session_scope() as session:
        title = session.scalars(select(Title).where(Title.arr_id == 99)).one()
        assert title.enabled is False and title.tvdb_id == 5


def test_movie_added_is_spelled_differently_and_still_works(client: TestClient, token: str) -> None:
    """Sonarr says `SeriesAdd`, Radarr says `MovieAdded`; the table must not assume
    symmetry."""
    post(
        client,
        token,
        {"eventType": "MovieAdded", "movie": {"id": 7, "title": "Film", "tmdbId": 3}},
        app="radarr",
    )
    with session_scope() as session:
        title = session.scalars(select(Title).where(Title.kind == "movie")).one()
        assert title.arr_id == 7 and title.enabled is False


def test_a_rename_moves_the_item_by_file_id(client: TestClient, token: str, mapped, series) -> None:
    """Keyed on `arr_file_id`, which is authoritative. This is also why a restore
    must target the item's current path rather than `backups.original_path`."""
    post(client, token, download())
    new_path = "/tv/Show/Season 01/Show - S01E01 - Pilot.mkv"

    response = post(
        client,
        token,
        {
            "eventType": "Rename",
            "series": {"id": 42},
            "renamedEpisodeFiles": [{"id": 501, "path": new_path, "previousPath": E01}],
        },
    )
    assert response.status_code == 200
    assert len(jobs()) == 1, "a rename never enqueues"
    with session_scope() as session:
        assert session.scalars(select(MediaItem)).one().path == (
            "/media/tv/Show/Season 01/Show - S01E01 - Pilot.mkv"
        )


def test_a_rename_falls_back_to_the_previous_path(
    client: TestClient, token: str, mapped, series
) -> None:
    post(client, token, download())
    response = post(
        client,
        token,
        {
            "eventType": "Rename",
            "series": {"id": 42},
            "renamedEpisodeFiles": [{"path": "/tv/Show/new.mkv", "previousPath": E01}],
        },
    )
    assert response.status_code == 200
    with session_scope() as session:
        assert session.scalars(select(MediaItem)).one().path == "/media/tv/Show/new.mkv"


# ------------------------------------------------------------------ mapping


def test_identity_mapping_is_the_default(client: TestClient, token: str, series) -> None:
    with session_scope() as session:
        assert session.scalars(select(PathMapping)).all() == []
    post(client, token, download())
    with session_scope() as session:
        assert session.scalars(select(MediaItem)).one().path == E01


def test_a_dispatch_failure_is_recorded_and_still_answers_200(
    client: TestClient, token: str, monkeypatch
) -> None:
    import vidcleaner.api.webhooks as module

    def boom(*_a, **_kw):
        raise RuntimeError("something broke")

    monkeypatch.setattr(module, "_dispatch", boom)
    response = post(client, token, download())
    assert response.status_code == 200 and response.json()["ok"] is False
    assert events()[0].handled is False
    assert "something broke" in events()[0].note
