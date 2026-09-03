"""The ffmpeg tier's CI guard, which was inert.

`VIDCLEANER_TEST_REQUIRE_FFMPEG=1` exists so CI cannot go green by silently skipping
the whole integration tier -- "the classic way an arrangement like this rots", per the
conftest's own docstring. It rotted anyway: the guard used `pytest.mark.fail`, which is
not a pytest marker (there is `xfail`), and an unknown marker is ignored without
`--strict-markers`. So the flag did nothing at all.

This runs pytest in a subprocess, because that is the only way to assert what the flag
does to a *run*. It is the one test here that shells out, and it is worth it: the thing
under test is whether a green build means anything.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

CASE = """
def test_needs_ffmpeg(runner):
    assert runner is not None
"""


def run_pytest(tmp_path: Path, *, require: bool, ffmpeg: bool) -> subprocess.CompletedProcess:
    """A throwaway test under `tests/integration/`, so the directory rule marks it."""
    target = BACKEND / "tests" / "integration" / "test_ci_guard_probe.py"
    target.write_text(CASE)
    env = {
        "PATH": "/usr/bin:/bin" if ffmpeg else str(tmp_path / "empty"),
        "HOME": str(tmp_path),
        "VIDCLEANER_CONFIG_DIR": str(tmp_path / "config"),
    }
    if require:
        env["VIDCLEANER_TEST_REQUIRE_FFMPEG"] = "1"
    (tmp_path / "empty").mkdir(exist_ok=True)
    try:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(target),
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=BACKEND,
            env={**env, "PYTHONPATH": str(BACKEND)},
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        target.unlink(missing_ok=True)


def test_without_the_flag_a_missing_ffmpeg_skips(tmp_path: Path) -> None:
    """The default, so a developer with no ffmpeg still gets a useful run."""
    result = run_pytest(tmp_path, require=False, ffmpeg=False)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "skipped" in result.stdout


def test_with_the_flag_a_missing_ffmpeg_fails_the_run(tmp_path: Path) -> None:
    """The whole point: a green build must mean the tier actually ran."""
    result = run_pytest(tmp_path, require=False, ffmpeg=False)
    assert result.returncode == 0

    required = run_pytest(tmp_path, require=True, ffmpeg=False)
    assert required.returncode != 0, "the guard is inert again"
    assert "VIDCLEANER_TEST_REQUIRE_FFMPEG" in required.stdout


def test_fail_is_not_a_pytest_marker() -> None:
    """The specific mistake, stated so nobody reintroduces it. `--strict-markers` in
    pyproject.toml is the second line of defence."""
    import pytest

    assert not hasattr(pytest, "fail_marker")
    assert hasattr(pytest, "xfail"), "the marker the original author meant"
    assert callable(pytest.fail), "the function the guard uses now"
