"""Shared fixtures. Every test runs against a throwaway /config directory."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from functools import lru_cache
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


# --------------------------------------------------------------------- ffmpeg

FFMPEG_MIN_VERSION = (7, 0)


@lru_cache(maxsize=1)
def ffmpeg_status() -> tuple[bool, str]:
    """Is a usable ffmpeg present? Cached; probed at most once per session."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        return False, "ffmpeg is not on PATH"
    if shutil.which("ffprobe") is None:
        return False, "ffprobe is not on PATH"
    try:
        out = subprocess.run(
            [binary, "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"cannot run ffmpeg: {exc}"

    match = re.search(r"version\s+n?(\d+)\.(\d+)", out.splitlines()[0] if out else "")
    if match is None:
        return False, "cannot parse the ffmpeg version"
    version = (int(match.group(1)), int(match.group(2)))
    if version < FFMPEG_MIN_VERSION:
        return False, f"ffmpeg {version[0]}.{version[1]} < {FFMPEG_MIN_VERSION[0]}.0"
    return True, f"ffmpeg {version[0]}.{version[1]}"


def pytest_collection_modifyitems(config, items):
    """Auto-mark everything under tests/integration/ and skip when ffmpeg is absent.

    Marking by directory means nobody has to remember the decorator. Setting
    VIDCLEANER_TEST_REQUIRE_FFMPEG=1 turns the skips into failures, so CI cannot
    go green by silently skipping the entire integration tier -- the classic way
    an arrangement like this rots.
    """
    ok, why = ffmpeg_status()
    required = os.environ.get("VIDCLEANER_TEST_REQUIRE_FFMPEG") == "1"
    for item in items:
        parts = Path(str(item.fspath)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.ffmpeg)
        if "contract" in parts:
            # Marked by directory so `-m contract` selects the tier; it needs no
            # ffmpeg and no network, so it is never skipped.
            item.add_marker(pytest.mark.contract)
        if "ffmpeg" in item.keywords and not ok:
            if required:
                item.add_marker(pytest.mark.fail(reason=why))
            else:
                item.add_marker(pytest.mark.skip(reason=f"ffmpeg unavailable: {why}"))
