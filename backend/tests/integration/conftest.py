"""Session-scoped synthetic media for the ffmpeg-dependent tier.

Built once per session from ffmpeg's own generators (~2 s total), into a temp
directory. Tests that render copy what they need into their own ``tmp_path``
first, so swap/backup behaviour can be exercised destructively.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.make_fixtures import FixtureSet, build_all


@pytest.fixture(scope="session")
def fixture_media(tmp_path_factory) -> FixtureSet:
    return build_all(tmp_path_factory.mktemp("media"))


@pytest.fixture
def sample_mkv(fixture_media: FixtureSet, tmp_path: Path) -> Path:
    """A private copy of the main fixture, safe to mutate."""
    target = tmp_path / fixture_media.sample_mkv.name
    shutil.copy2(fixture_media.sample_mkv, target)
    return target


@pytest.fixture
def sample_mp4(fixture_media: FixtureSet, tmp_path: Path) -> Path:
    target = tmp_path / fixture_media.sample_mp4.name
    shutil.copy2(fixture_media.sample_mp4, target)
    return target


@pytest.fixture
def runner(tmp_path: Path):
    from vidcleaner.pipeline.ffmpeg import FFmpegRunner

    return FFmpegRunner(log_path=tmp_path / "ffmpeg.log")
