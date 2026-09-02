"""Auto-promotion to full mode, and drift measured on real media (PLAN.md §6).

Both paths need a real container -- the decision is made from ffprobe output and
a real subtitle extraction -- but the fixture audio is a sine tone, so recognition
comes from ``ScriptedTranscriber``. That is the honest split: ffmpeg does the
part only ffmpeg can do, and the words are supplied.
"""

from __future__ import annotations

import pytest

from vidcleaner.matching.compiler import build_matcher
from vidcleaner.pipeline.artifacts import (
    DriftResult,
    ProbeResult,
    ProfileSnapshot,
    SubtitlesResult,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from vidcleaner.pipeline.stages import build_context, build_spec, run_stage
from vidcleaner.pipeline.stt import ScriptedTranscriber, resolve_mode
from vidcleaner.settings_store import AppSettings

#: `tests/fixtures/marked.srt`, at the times the audio really speaks them.
SPOKEN = [
    ("Oh", 0.55),
    ("shit", 0.70),
    ("that", 1.00),
    ("hurt", 1.25),
    ("You", 2.05),
    ("fucking", 2.20),
    ("idiot", 2.70),
    ("Nothing", 3.55),
    ("to", 3.85),
    ("see", 3.95),
    ("in", 4.15),
    ("Scunthorpe", 4.25),
    ("God", 5.05),
    ("damn", 5.30),
    ("it", 5.65),
    ("He", 6.55),
    ("was", 6.70),
    ("friggin", 6.85),
    ("tired", 7.15),
    ("Bullshit", 8.05),
    ("Bull", 8.45),
    ("Shit", 8.75),
]


def spoken_transcript() -> Transcript:
    words = [TranscriptWord(word=w, start=t, end=t + 0.25) for w, t in SPOKEN]
    return Transcript(
        segments=[TranscriptSegment(start=words[0].start, end=words[-1].end, words=words)]
    )


@pytest.fixture
def nosubs_mkv(fixture_media):
    return fixture_media.nosubs_mkv


@pytest.fixture
def drift_mkv(fixture_media):
    return fixture_media.drift_mkv


def run_to_subtitles(source, settings_deploy, **settings_kw):
    spec = build_spec(
        source,
        profile=ProfileSnapshot(profile_hash="v1:test"),
        settings=AppSettings(**settings_kw),
        stt_mode=settings_kw.pop("stt_mode", "windowed"),
    )
    ctx = build_context(spec, deploy=settings_deploy)
    ctx.matcher = build_matcher()
    ctx.transcriber = ScriptedTranscriber(spoken_transcript())
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    return ctx


# ------------------------------------------------- a file with no subtitles


def test_a_file_with_no_subtitles_is_promoted_to_a_full_pass(settings, nosubs_mkv):
    """M1's silent failure: no subtitles meant no windows meant no detections,
    and the file came back looking clean."""
    ctx = run_to_subtitles(nosubs_mkv, settings, drift_check=False)
    subs = SubtitlesResult.read(ctx.ws.subs_json)
    probe = ProbeResult.read(ctx.ws.probe_json)
    assert subs.cues == []
    assert subs.source.kind == "none"

    mode, reason = resolve_mode("windowed", subs, duration_s=probe.duration, settings=ctx.settings)
    assert (mode, reason) == ("full", "no_subtitles")


def test_the_full_pass_actually_transcribes_and_detects(settings, nosubs_mkv):
    ctx = run_to_subtitles(nosubs_mkv, settings, drift_check=False)
    run_stage(ctx, "transcribe")
    transcript = Transcript.read(ctx.ws.transcript_json)
    assert transcript.mode == "full"
    assert transcript.mode_reason == "no_subtitles"
    assert transcript.windows == [], "a full pass must not be windowed"

    run_stage(ctx, "detect")
    from vidcleaner.pipeline.detect import load as load_detections

    found = {d.word_canonical for d in load_detections(ctx.ws).detections}
    assert {"shit", "fuck", "bullshit"} <= found
    assert all(d.source == "stt" for d in load_detections(ctx.ws).detections)


def test_the_hour_cap_refuses_the_promotion_and_says_so(settings, nosubs_mkv):
    ctx = run_to_subtitles(nosubs_mkv, settings, drift_check=False, stt_full_max_hours=0.001)
    run_stage(ctx, "transcribe")
    transcript = Transcript.read(ctx.ws.transcript_json)
    assert transcript.mode_reason == "full_skipped_too_long"
    assert transcript.word_count == 0


# -------------------------------------------------- a file with drifted subs


def test_drifted_subtitles_are_measured_on_real_media(settings, drift_mkv):
    """`sample_drift.mkv` carries `marked.srt` shifted +2 s against its audio."""
    from scripts.make_fixtures import DRIFT_SHIFT_S

    ctx = run_to_subtitles(drift_mkv, settings)
    drift = DriftResult.read(ctx.ws.drift_json)

    assert drift.checked
    # The words are spoken *earlier* than the cues claim, so the offset is negative.
    assert drift.offset_s == pytest.approx(-DRIFT_SHIFT_S, abs=0.2)
    assert drift.action == "unreliable"
    assert drift.coverage > 0.5


def test_a_drift_verdict_widens_and_moves_the_windows(settings, drift_mkv):
    ctx = run_to_subtitles(drift_mkv, settings)
    subs = SubtitlesResult.read(ctx.ws.subs_json)

    assert not subs.reliable
    assert subs.usable, "the cues describe this audio; only their timing is wrong"
    assert subs.window_pad_s == ctx.settings.drift_window_pad_s
    assert subs.windows, "widened windows, not none"
    # Uncorrected, a window built on the +2 s cue would start around 3.5 s.
    # Corrected and widened it must reach back to where the word is actually said.
    assert subs.windows[0].start < 1.0


def test_the_drift_artifact_records_its_evidence(settings, drift_mkv):
    ctx = run_to_subtitles(drift_mkv, settings)
    drift = DriftResult.read(ctx.ws.drift_json)
    assert drift.probes
    for probe in drift.probes:
        assert probe.cue_text
        assert probe.span is not None
