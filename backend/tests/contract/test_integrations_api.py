"""The M3 API surface: Test buttons, path mappings, Sync now."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from vidcleaner.db.models import PathMapping
from vidcleaner.db.session import session_scope


def test_an_unconfigured_integration_reports_why(client: TestClient) -> None:
    body = client.post("/api/integrations/sonarr/test").json()
    assert body["ok"] is False
    assert "no URL or API key" in body["detail"]


def test_an_unknown_integration_is_a_404(client: TestClient) -> None:
    assert client.post("/api/integrations/plex/test").status_code == 404


def test_a_masked_key_in_the_body_falls_through_to_the_stored_one(client: TestClient) -> None:
    """The API returns `***` for secrets, so the form may never have seen the real
    value -- posting it back must not be read as "test with the literal ***"."""
    client.patch("/api/settings", json={"sonarr_url": "http://127.0.0.1:1", "sonarr_api_key": "k"})
    body = client.post("/api/integrations/sonarr/test", json={"api_key": "***"}).json()
    # 127.0.0.1:1 refuses, so this is a reachability failure, not "not configured".
    assert body["ok"] is False
    assert "no URL or API key" not in body["detail"]


def test_path_mappings_round_trip(client: TestClient) -> None:
    assert client.get("/api/path-mappings").json() == []
    payload = [
        {"app": "sonarr", "from_prefix": "/tv/", "to_prefix": "/media/tv"},
        {"app": "jellyfin", "from_prefix": "/data", "to_prefix": "/media"},
    ]
    response = client.put("/api/path-mappings", json=payload)
    assert response.status_code == 200
    rows = response.json()
    assert {r["app"] for r in rows} == {"sonarr", "jellyfin"}
    # Trailing separators are normalised on the way in.
    assert [r["from_prefix"] for r in rows if r["app"] == "sonarr"] == ["/tv"]
    assert client.get("/api/path-mappings").json() == rows


def test_replacing_the_mappings_removes_the_old_ones(client: TestClient) -> None:
    client.put(
        "/api/path-mappings",
        json=[{"app": "sonarr", "from_prefix": "/tv", "to_prefix": "/media/tv"}],
    )
    client.put("/api/path-mappings", json=[])
    assert client.get("/api/path-mappings").json() == []


def test_a_duplicate_prefix_is_rejected_before_anything_is_written(client: TestClient) -> None:
    client.put(
        "/api/path-mappings",
        json=[{"app": "sonarr", "from_prefix": "/tv", "to_prefix": "/media/tv"}],
    )
    response = client.put(
        "/api/path-mappings",
        json=[
            {"app": "sonarr", "from_prefix": "/tv", "to_prefix": "/media/tv"},
            {"app": "sonarr", "from_prefix": "/shows", "to_prefix": "/media/tv"},
        ],
    )
    assert response.status_code == 422 and "duplicate" in response.json()["detail"]
    with session_scope() as session:
        assert len(session.scalars(select(PathMapping)).all()) == 1, "the old table survives"


def test_an_unknown_app_is_rejected(client: TestClient) -> None:
    response = client.put(
        "/api/path-mappings", json=[{"app": "plex", "from_prefix": "/a", "to_prefix": "/b"}]
    )
    assert response.status_code == 422


def test_sync_now_needs_an_arr(client: TestClient) -> None:
    response = client.post("/api/library/sync")
    assert response.status_code == 409
    assert "configured" in response.json()["detail"]


def test_syncing_one_title_reports_that_no_arr_is_configured(client: TestClient) -> None:
    """The UI calls this straight after the toggle, so it must answer even with
    nothing configured -- enabling a title is not an integration failure."""
    from tests.support.library import make_series

    title_id, _ = make_series(episodes=1)
    body = client.post(f"/api/library/titles/{title_id}/sync").json()
    assert body["items"] == 0
    assert body["errors"] == ["sonarr is not configured"]


def test_syncing_an_unknown_title_is_a_404(client: TestClient) -> None:
    assert client.post("/api/library/titles/999/sync").status_code == 404


def test_the_new_routes_are_registered_before_the_spa_catch_all(client: TestClient) -> None:
    """`_mount_spa` registers `GET /{full_path:path}`, which raises 404 for anything
    under `api/`. A router included after it would be shadowed, so this asserts the
    ordering rather than trusting it."""
    assert client.get("/api/path-mappings").status_code == 200
    assert client.post("/api/integrations/sonarr/test").status_code == 200
    assert client.get("/api/no-such-endpoint").status_code == 404
    # A GET to a POST-only API path 404s rather than 405, because the catch-all is
    # GET-only and deliberately refuses everything under `api/`. Recorded here so
    # the behaviour reads as intended rather than as a routing bug.
    assert client.get("/api/integrations/sonarr/test").status_code == 404
