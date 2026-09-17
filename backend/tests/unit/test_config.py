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


# ------------------------------------------------------------------ backups_dir

# `rename(2)` refuses to cross mount points even when both sides are one filesystem,
# so the backups directory has to be a plain subdirectory of the media mount. These
# pin that it is derived rather than special-cased -- the `/backups` special case that
# used to be here resolved to a container-local directory whenever the entrypoint had
# created one, which made every swap fail EXDEV.


def test_backups_dir_is_derived_from_media_dir(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIDCLEANER_BACKUPS_DIR", raising=False)
    monkeypatch.setenv("VIDCLEANER_MEDIA_DIR", "/library")
    get_settings.cache_clear()
    assert get_settings().backups_dir == Path("/library/VidCleaner-Backups")


def test_a_media_dir_from_settings_json_is_followed_too(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIDCLEANER_BACKUPS_DIR", raising=False)
    monkeypatch.delenv("VIDCLEANER_MEDIA_DIR", raising=False)
    (config_dir / "settings.json").write_text(json.dumps({"media_dir": "/srv/films"}))
    get_settings.cache_clear()
    assert get_settings().backups_dir == Path("/srv/films/VidCleaner-Backups")


def test_the_old_hidden_default_is_retired_even_when_pinned_by_env(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image, compose and the unraid template all shipped that literal, and an
    installed container keeps the variable it was created with -- so for exactly the
    installs that need to move, the old path is pinned from outside. Treating that one
    value as unset is what lets an upgrade happen at all."""
    monkeypatch.setenv("VIDCLEANER_MEDIA_DIR", "/library")
    monkeypatch.setenv("VIDCLEANER_BACKUPS_DIR", "/library/.vidcleaner-backups")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.backups_dir == Path("/library/VidCleaner-Backups")
    assert settings.legacy_backups_dir == Path("/library/.vidcleaner-backups")


def test_a_deliberate_override_is_honoured(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDCLEANER_MEDIA_DIR", "/library")
    monkeypatch.setenv("VIDCLEANER_BACKUPS_DIR", "/mnt/elsewhere/keep")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.backups_dir == Path("/mnt/elsewhere/keep")
    assert settings.backups_dir_is_hidden is False


def test_a_hidden_backups_dir_is_reported_so_the_ui_can_say_so(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDCLEANER_BACKUPS_DIR", "/mnt/elsewhere/.keep")
    get_settings.cache_clear()
    assert get_settings().backups_dir_is_hidden is True
