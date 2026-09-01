"""Operational settings: defaults, partial updates, and secret handling (§5)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from vidcleaner.config import Settings
from vidcleaner.db.models import Setting
from vidcleaner.db.session import session_scope
from vidcleaner.settings_store import MASK, AppSettings, load_settings, save_settings


def test_defaults_match_the_plan(migrated: Settings) -> None:
    with session_scope() as session:
        settings = load_settings(session)
    assert settings.stt_windowed_model == "large-v3-turbo"
    assert settings.stt_full_model == "medium"
    assert settings.audit_pass == "idle"
    assert (settings.pad_pre_ms, settings.pad_post_ms) == (80, 120)
    assert settings.backup_retention_days == 30


def test_partial_update_leaves_other_values_alone(migrated: Settings) -> None:
    with session_scope() as session:
        save_settings(session, {"pad_pre_ms": 150})
    with session_scope() as session:
        settings = load_settings(session)
    assert settings.pad_pre_ms == 150
    assert settings.pad_post_ms == 120


def test_secrets_are_encrypted_at_rest(migrated: Settings) -> None:
    with session_scope() as session:
        save_settings(session, {"sonarr_api_key": "super-secret"})

    with session_scope() as session:
        stored = session.execute(
            select(Setting).where(Setting.key == "sonarr_api_key")
        ).scalar_one()
        raw = json.loads(stored.value_json)
        assert "super-secret" not in raw
        assert raw.startswith("enc:v1:")
        assert load_settings(session).sonarr_api_key == "super-secret"


def test_writing_the_mask_back_does_not_clear_the_secret(migrated: Settings) -> None:
    with session_scope() as session:
        save_settings(session, {"sonarr_api_key": "keep-me"})
    with session_scope() as session:
        save_settings(session, {"sonarr_api_key": MASK, "radarr_url": "http://radarr:7878"})
    with session_scope() as session:
        settings = load_settings(session)
    assert settings.sonarr_api_key == "keep-me"
    assert settings.radarr_url == "http://radarr:7878"


def test_unknown_keys_in_the_table_are_ignored(migrated: Settings) -> None:
    with session_scope() as session:
        session.add(Setting(key="removed_in_a_later_version", value_json='"x"'))
    with session_scope() as session:
        assert isinstance(load_settings(session), AppSettings)


def test_api_round_trip_masks_secrets(client: TestClient) -> None:
    assert client.get("/api/settings").json()["sonarr_api_key"] == ""

    response = client.patch(
        "/api/settings", json={"sonarr_api_key": "abc123", "audit_pass": "always"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sonarr_api_key"] == MASK
    assert body["audit_pass"] == "always"

    assert client.get("/api/settings").json()["sonarr_api_key"] == MASK


def test_api_rejects_bad_values(client: TestClient) -> None:
    assert client.patch("/api/settings", json={"audit_pass": "sometimes"}).status_code == 422
    assert client.patch("/api/settings", json={"beam_size": 99}).status_code == 422
    assert client.patch("/api/settings", json={"not_a_setting": 1}).status_code == 422
    # A rejected patch must not have written anything.
    assert client.get("/api/settings").json()["audit_pass"] == "idle"
