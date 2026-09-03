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
    """Mark the two tiers by directory, so nobody has to remember a decorator.

    Whether a marked test then runs, skips or fails is
    :func:`pytest_runtest_setup`'s decision -- see the note there about why it cannot
    be made here.
    """
    for item in items:
        parts = Path(str(item.fspath)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.ffmpeg)
        if "contract" in parts:
            # Marked by directory so `-m contract` selects the tier; it needs no
            # ffmpeg and no network, so it is never skipped.
            item.add_marker(pytest.mark.contract)


def _guard(item, marker: str, status, env_var: str) -> None:
    """Skip a marked test when its tool is missing -- or fail it if CI said so.

    Factored out so the ``ocr`` tier gets the same protection as ``ffmpeg``
    rather than a weaker one: a silently-skipped OCR tier is exactly the rot
    described below, and M6 would be the second time it happened.
    """
    if marker not in item.keywords:
        return
    ok, why = status()
    if ok:
        return
    if os.environ.get(env_var) == "1":
        pytest.fail(f"{env_var}=1 but {marker} is unusable: {why}")
    pytest.skip(f"{marker} unavailable: {why}")


def pytest_runtest_setup(item):
    """Skip an ffmpeg test when ffmpeg is unusable -- or **fail** it if CI said so.

    ``VIDCLEANER_TEST_REQUIRE_FFMPEG=1`` turns the skips into failures so a build
    cannot go green while silently skipping the entire integration tier -- "the classic
    way an arrangement like this rots", as this file has said since M1. It rotted
    anyway, in two layers:

    1. the guard used ``pytest.mark.fail``, which is **not a pytest marker** (there is
       ``xfail``), and an unknown marker is ignored without ``--strict-markers``
       (now set in `pyproject.toml`);
    2. and it was applied in ``pytest_collection_modifyitems``, which is too late for
       ``usefixtures`` -- the item's fixture closure is already computed by then, so
       even a *correct* fixture marker added there does nothing.

    This hook is the version that works: it runs per test, at setup, before any fixture,
    and can call `pytest.fail` or `pytest.skip` directly. `tests/unit/test_ci_guard.py`
    runs pytest in a subprocess to prove it, because that is the only way to assert
    what the flag does to a *run*.
    """
    _guard(item, "ffmpeg", ffmpeg_status, "VIDCLEANER_TEST_REQUIRE_FFMPEG")
    _guard(item, "ocr", ocr_status, "VIDCLEANER_TEST_REQUIRE_OCR")


@lru_cache(maxsize=1)
def ocr_status() -> tuple[bool, str]:
    """Is OCR usable? Same shape as :func:`ffmpeg_status`, same reason."""
    from vidcleaner.pipeline import ocr

    if not ocr.is_available():
        return False, "tesseract or the `ocr` extra is missing"
    return True, "tesseract present"
