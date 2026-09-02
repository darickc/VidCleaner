"""GET /api/health backs both the container healthcheck and the UI Queue page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from vidcleaner import __version__
from vidcleaner.api import health as health_module
from vidcleaner.db.migrate import alembic_config


def current_head() -> str:
    """The head revision, read from Alembic rather than hardcoded.

    A literal "0001" here made every future migration break an unrelated test.
    """
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None
    return head


def test_health_reports_database_and_disk(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()

    assert body["version"] == __version__
    assert body["role"] == "all"
    assert body["database"]["ok"] is True
    assert body["database"]["revision"] == current_head()
    assert set(body["disk"]) == {"config", "media", "backups", "work"}
    assert body["disk"]["work"]["free_bytes"] > 0


def test_status_is_degraded_without_ffmpeg(client: TestClient) -> None:
    health_module.ffmpeg_info.cache_clear()
    try:
        expected = "ok" if health_module.ffmpeg_info()["present"] else "degraded"
        assert client.get("/api/health").json()["status"] == expected
    finally:
        health_module.ffmpeg_info.cache_clear()


def test_database_failure_reports_503(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        health_module, "_database", lambda: {"ok": False, "error": "boom", "revision": None}
    )
    response = client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["status"] == "error"


def test_unknown_api_path_is_404_not_the_spa(client: TestClient) -> None:
    assert client.get("/api/does-not-exist").status_code == 404


def test_spa_placeholder_when_frontend_is_not_built(client: TestClient) -> None:
    response = client.get("/library")
    assert response.status_code == 200
    assert "VidCleaner" in response.text
