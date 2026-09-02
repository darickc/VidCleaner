"""The lazy-import boundary that keeps ffmpeg work free of the 2 GB STT stack.

PLAN.md's M1 render/verify integration tests must run on a checkout without
`uv sync --extra stt`. That only holds if `faster_whisper`/`whisperx`/`torch`
are imported inside functions, never at module scope -- so it is asserted here
rather than left as an aspiration.
"""

from __future__ import annotations

import subprocess
import sys

HEAVY = ("torch", "faster_whisper", "whisperx", "ctranslate2", "transformers")

PROBE = """
import sys
import {module}
bad = sorted(m for m in {heavy!r} if m in sys.modules)
print(",".join(bad))
sys.exit(1 if bad else 0)
"""


def _assert_clean(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(module=module, heavy=HEAVY)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"importing {module} pulled in {result.stdout.strip()}; "
        "move the heavy import inside the function that needs it"
    )


def test_pipeline_package_is_light():
    _assert_clean("vidcleaner.pipeline")


def test_drift_is_light():
    """The drift check *uses* a transcriber but must not import one: it runs
    inside the subtitles stage, which the API imports through the registry."""
    _assert_clean("vidcleaner.pipeline.drift")


def test_subtitles_is_light():
    _assert_clean("vidcleaner.pipeline.subtitles")


def test_stage_driver_is_light():
    _assert_clean("vidcleaner.pipeline.stages")


def test_artifacts_are_light():
    _assert_clean("vidcleaner.pipeline.artifacts")


def test_matching_engine_is_light():
    _assert_clean("vidcleaner.matching.compiler")


def test_api_app_is_light():
    _assert_clean("vidcleaner.main")


def test_cli_is_light():
    _assert_clean("vidcleaner.cli")
