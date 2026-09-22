"""`mute_window_silent`: telling a mute that never ran from a codec edge artefact.

A single peak cannot distinguish them -- both read well above `INAUDIBLE_DB` --
so the check escalates to `silencedetect` and asks what share of the range is
actually silent. Only the first case is worth refusing to swap over.
"""

from __future__ import annotations

from pathlib import Path

from vidcleaner.pipeline.artifacts import TimeRange
from vidcleaner.pipeline.ffmpeg import SilenceSpan, VolumeStats
from vidcleaner.pipeline.verify import WINDOW_INSET_S, _acoustic_checks

# One 1.0 s mute, comfortably longer than MIN_PROBE_WINDOW_S.
MUTE = TimeRange(start=10.0, end=11.0)


class StubRunner:
    """Answers the two measurements `_acoustic_checks` makes, and records them."""

    def __init__(self, *, peak_db: float, silence: tuple[SilenceSpan, ...] = ()):
        self.peak_db = peak_db
        self.silence = silence
        self.silence_calls: list[TimeRange] = []

    def decode_check(self, path, *, stream="0:a:0"):
        return ""

    def measure_volume(self, path, *, stream="0:a:0", window=None, label="volumedetect"):
        # The control window sits far from the mute and must read as audible.
        if window is not None and window.start > 20.0:
            return VolumeStats(-20.0, -11.0, 48_000)
        return VolumeStats(self.peak_db - 10.0, self.peak_db, 48_000)

    def detect_silence(
        self, path, *, stream="0:a:0", threshold_db=-60.0, min_duration=0.02, window=None, label=""
    ):
        self.silence_calls.append(window)
        return list(self.silence)


class Plan:
    duration = 60.0


def run(runner):
    checks, _ = _acoustic_checks(runner, Path("/work/out.mkv"), Plan(), [MUTE])
    return {c.name: c for c in checks}


def test_a_silent_window_never_pays_for_the_escalation():
    runner = StubRunner(peak_db=-91.0)
    result = run(runner)
    assert result["mute_window_silent[0]"].ok
    assert runner.silence_calls == []


def test_an_edge_artefact_passes_and_records_the_coverage():
    """The mute ran; a few ms leaked in at one edge. 0.96 s of 1.0 s is silent."""
    runner = StubRunner(peak_db=-18.0, silence=(SilenceSpan(0.29, 1.25),))
    result = run(runner)
    check = result["mute_window_silent[0]"]
    assert check.ok, check.detail
    assert "96% of the range is silent" in check.detail
    # Scoped to the range plus audible context on both sides, not the whole file.
    assert runner.silence_calls[0].start < MUTE.start
    assert runner.silence_calls[0].end > MUTE.end


def test_a_mute_that_never_landed_is_still_fatal():
    runner = StubRunner(peak_db=-18.0, silence=())
    check = run(runner)["mute_window_silent[0]"]
    assert not check.ok
    assert check.severity == "fatal"
    assert "0% of the range is silent" in check.detail


def test_partial_coverage_below_the_bar_is_fatal():
    """Half the range silent means half of a word went out audible."""
    runner = StubRunner(peak_db=-18.0, silence=(SilenceSpan(0.25, 0.75),))
    check = run(runner)["mute_window_silent[0]"]
    assert not check.ok
    assert check.severity == "fatal"


def test_the_detail_names_the_mute_range_not_just_the_index():
    """The index is positional in a twice-sorted list, so it identifies nothing."""
    check = run(StubRunner(peak_db=-91.0))["mute_window_silent[0]"]
    assert f"{MUTE.start:.2f}-{MUTE.end:.2f}s" in check.detail


def test_a_missing_measurement_names_the_range_too():
    class NoMeasurement(StubRunner):
        def measure_volume(self, path, *, stream="0:a:0", window=None, label="volumedetect"):
            return None if window.start < 20.0 else VolumeStats(-20.0, -11.0, 48_000)

    check = run(NoMeasurement(peak_db=-91.0))["mute_window_silent[0]"]
    assert not check.ok
    assert f"{MUTE.start:.2f}-{MUTE.end:.2f}s" in check.detail


def test_the_nominal_range_is_the_window_widened_back_out():
    """`_acoustic_checks` reconstructs the mute range by undoing the inset."""
    runner = StubRunner(peak_db=-18.0, silence=(SilenceSpan(0.25, 1.25),))
    run(runner)
    scope = runner.silence_calls[0]
    assert scope.start < MUTE.start - WINDOW_INSET_S
