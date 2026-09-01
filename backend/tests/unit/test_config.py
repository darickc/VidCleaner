"""Deployment config layering: env > settings.json > defaults (PLAN.md §4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidcleaner.config import Settings, get_settings


def test_defaults_without_container_mounts(config_dir: Path) -> None:
    settings = get_settings()
    assert settings.role == "all"
    assert settings.port == 8585
    assert settings.runs_api and settings.runs_worker


def test_database_url_derives_from_config_dir(config_dir: Path) -> None:
    assert get_settings().database_url.endswith(str(config_dir / "vidcleaner.db"))


def test_db_url_override_wins(monkeypatch: pytest.MonkeyPatch, config_dir: Path) -> None:
    monkeypatch.setenv("VIDCLEANER_DB_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()
    assert get_settings().database_url == "sqlite+pysqlite:///:memory:"


def test_settings_json_is_read(config_dir: Path) -> None:
    (config_dir / "settings.json").write_text(json.dumps({"port": 9000, "log_level": "DEBUG"}))
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.port == 9000
    assert settings.log_level == "DEBUG"


def test_env_beats_settings_json(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (config_dir / "settings.json").write_text(json.dumps({"port": 9000}))
    monkeypatch.setenv("VIDCLEANER_PORT", "7777")
    get_settings.cache_clear()
    assert get_settings().port == 7777


def test_malformed_settings_json_falls_back_to_defaults(config_dir: Path) -> None:
    (config_dir / "settings.json").write_text("{not json")
    get_settings.cache_clear()
    assert get_settings().port == 8585


@pytest.mark.parametrize(
    ("role", "api", "worker"),
    [("all", True, True), ("api", True, False), ("worker", False, True)],
)
def test_role_flags(role: str, api: bool, worker: bool) -> None:
    settings = Settings(role=role)
    assert settings.runs_api is api
    assert settings.runs_worker is worker
