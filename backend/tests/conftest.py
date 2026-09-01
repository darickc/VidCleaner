"""Shared fixtures. Every test runs against a throwaway /config directory."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vidcleaner.config import Settings, get_settings
from vidcleaner.crypto import get_secret_box
from vidcleaner.db.migrate import upgrade_to_head
from vidcleaner.db.session import reset_engine_cache


def _reset_caches() -> None:
    get_settings.cache_clear()
    get_secret_box.cache_clear()
    reset_engine_cache()


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every path at tmp_path so tests never touch the developer's data."""
    for name, sub in (
        ("CONFIG_DIR", "config"),
        ("WORK_DIR", "work"),
        ("BACKUPS_DIR", "backups"),
        ("MEDIA_DIR", "media"),
        ("STATIC_DIR", "static"),
    ):
        (tmp_path / sub).mkdir(exist_ok=True)
        monkeypatch.setenv(f"VIDCLEANER_{name}", str(tmp_path / sub))
    _reset_caches()
    yield tmp_path / "config"
    _reset_caches()


@pytest.fixture
def settings(config_dir: Path) -> Settings:
    return get_settings()


@pytest.fixture
def migrated(settings: Settings) -> Settings:
    upgrade_to_head(settings)
    return settings


@pytest.fixture
def client(migrated: Settings) -> Iterator[TestClient]:
    from vidcleaner.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
