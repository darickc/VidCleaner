"""Drift wired into the subtitles stage (PLAN.md §6 step 3).

Driven by ``ScriptedTranscriber``, which filters by window, so the probe-span
plumbing is genuinely exercised rather than bypassed.
"""

from __future__ import annotations

import pytest

from vidcleaner.matching.compiler import build_matcher
from vidcleaner.pipeline import subtitles as subtitles_stage
from vidcleaner.pipeline.artifacts import (
    AudioStreamInfo,
    CodecPlan,
    DriftResult,
    ProbeResult,
    ProfileSnapshot,
    SubtitleCue,
    SubtitlesResult,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from vidcleaner.pipeline.drift import cue_word_spans
from vidcleaner.pipeline.stages import build_context, build_spec
from vidcleaner.pipeline.stt import ScriptedTranscriber
from vidcleaner.settings_store import AppSettings

CUE_TEXTS = [
    "the quick brown fox jumps over the lazy dog",
    "she sells seashells beside the noisy seashore today",
    "pack my box with five dozen liquor jugs please",
]


@pytest.fixture
def deploy(tmp_path):
    from vidcleaner.config import Settings

    return Settings(config_dir=tmp_path / "config", work_dir=tmp_path / "work")


def cues() -> list[SubtitleCue]:
    return [
        SubtitleCue(index=i, start=100.0 + i * 300.0, end=100.0 + i * 300.0 + 3.0, text=text)
        for i, text in enumerate(CUE_TEXTS)
    ]


def heard(delta: float, *, start_time: float = 0.0) -> Transcript:
    """The cue words, spoken ``delta`` seconds later than the cues claim.

    ``start_time`` mirrors what a transcriber returns for a container whose audio
    stream starts late: the words come back already in container time.
    """
    words = []
    for cue in cues():
        for word in cue_word_spans(cue):
            words.append(
                TranscriptWord(word=word.fold, start=word.start + delta, end=word.end + delta)
            )
    return Transcript(
        segments=[TranscriptSegment(start=words[0].start, end=words[-1].end, words=words)],
        audio_start_offset_s=start_time,
    )


def make_ctx(deploy, tmp_path, *, transcript: Transcript | None, start_time: float = 0.0, **kw):
    source = tmp_path / "x.mkv"
    source.write_bytes(b"")
    spec = build_spec(
        source,
        profile=ProfileSnapshot(profile_hash="v1:x"),
        settings=AppSettings(**kw),
    )
    ctx = build_context(spec, deploy=deploy, runner=object())
    ctx.matcher = build_matcher()
    if transcript is not None:
        ctx.transcriber = ScriptedTranscriber(transcript)
    ProbeResult(
        path=str(source),
        size=1,
        mtime=0.0,
        duration=1200.0,
        audio=[AudioStreamInfo(index=0, typed_index=0, codec_name="ac3", start_time=start_time)],
        clean_codec=CodecPlan(encoder="ac3", bit_rate=640000),
    ).write(ctx.ws.probe_json)
    ctx.ws.audio_wav.write_bytes(b"RIFF")  # measure_drift only checks it exists
    return ctx


def run_drift(ctx, cue_list=None) -> DriftResult:
    from vidcleaner.pipeline.artifacts import ProbeResult as PR

    chosen = cues() if cue_list is None else cue_list
    return subtitles_stage._measure_drift(ctx, chosen, PR.read(ctx.ws.probe_json))


# --------------------------------------------------------------- measurement


def test_a_shifted_subtitle_track_is_measured(deploy, tmp_path):
    ctx = make_ctx(deploy, tmp_path, transcript=heard(2.5))
    result = run_drift(ctx)
    assert result.checked
    assert result.offset_s == pytest.approx(2.5, abs=0.05)
    assert result.action == "unreliable"
    assert result.coverage > 0.9


def test_a_well_timed_track_measures_near_zero(deploy, tmp_path):
    ctx = make_ctx(deploy, tmp_path, transcript=heard(0.05))
    result = run_drift(ctx)
    assert result.action == "ok"
    assert abs(result.offset_s) < 0.2


def test_unrelated_audio_is_discarded(deploy, tmp_path):
    """Wrong episode or wrong language: the cues do not describe this audio."""
    words = [TranscriptWord(word=f"zzz{i}", start=100.0 + i, end=100.5 + i) for i in range(40)]
    transcript = Transcript(segments=[TranscriptSegment(start=100.0, end=140.0, words=words)])
    ctx = make_ctx(deploy, tmp_path, transcript=transcript)
    assert run_drift(ctx).action == "discard"


def test_the_audio_stream_offset_is_not_mistaken_for_drift(deploy, tmp_path):
    """The tripwire this whole module needs.

    On a container whose audio starts at +0.5 s the transcriber returns words
    already in container time. If the probe spans or the returned words were
    handled on the wrong clock, drift would measure -0.5 s and "correct" a drift
    that does not exist -- silently, on exactly the TS-derived rips ONE CLOCK
    exists for.
    """
    ctx = make_ctx(deploy, tmp_path, transcript=heard(0.0, start_time=0.5), start_time=0.5)
    result = run_drift(ctx)
    assert abs(result.offset_s) < 0.1, f"measured {result.offset_s}, expected ~0"
    assert result.action == "ok"


# ------------------------------------------------------------------ skipping


def test_the_check_can_be_turned_off(deploy, tmp_path):
    ctx = make_ctx(deploy, tmp_path, transcript=heard(2.5), drift_check=False)
    result = run_drift(ctx)
    assert (result.checked, result.reason) == (False, "disabled")


def test_no_cues_means_no_check(deploy, tmp_path):
    ctx = make_ctx(deploy, tmp_path, transcript=heard(2.5))
    assert run_drift(ctx, []).reason == "no_cues"


def test_missing_audio_is_reported_not_raised(deploy, tmp_path):
    ctx = make_ctx(deploy, tmp_path, transcript=heard(2.5))
    ctx.ws.audio_wav.unlink()
    assert run_drift(ctx).reason == "no_audio"


def test_a_failing_transcriber_never_fails_the_job(deploy, tmp_path):
    """Losing the measurement costs precision; failing costs the episode."""

    class Broken:
        name = "broken"

        def transcribe(self, request, on_progress=None):
            raise RuntimeError("model exploded")

    ctx = make_ctx(deploy, tmp_path, transcript=None)
    ctx.transcriber = Broken()
    result = run_drift(ctx)
    assert (result.checked, result.reason) == (False, "stt_failed")


# ------------------------------------------------- effect on the stage output


def _subs_after(ctx, cue_list) -> SubtitlesResult:
    """Just the drift-dependent half of ``subtitles.run``."""
    from vidcleaner.pipeline.artifacts import ProbeResult as PR

    probe = PR.read(ctx.ws.probe_json)
    hits = subtitles_stage.find_hits(cue_list, ctx.matcher)
    result = run_drift(ctx, cue_list)
    pad = (
        ctx.settings.drift_window_pad_s
        if result.action == "unreliable"
        else subtitles_stage.WINDOW_PAD_S
    )
    usable = result.action != "discard"
    windows = (
        subtitles_stage.cue_windows(
            cue_list, hits, pad_s=pad, duration=probe.duration, offset_s=result.offset_s
        )
        if usable
        else []
    )
    return SubtitlesResult(
        cues=cue_list,
        hits=hits if usable else [],
        windows=windows,
        offset_s=result.offset_s if usable else 0.0,
        reliable=result.action != "unreliable",
        usable=usable,
        window_pad_s=pad,
    )


def test_unreliable_timing_widens_the_windows_and_moves_them(deploy, tmp_path):
    profane = cues()
    profane[0] = profane[0].model_copy(update={"text": CUE_TEXTS[0] + " shit"})
    ctx = make_ctx(deploy, tmp_path, transcript=heard(2.5))

    result = _subs_after(ctx, profane)
    assert not result.reliable
    assert result.window_pad_s == ctx.settings.drift_window_pad_s
    # The window is built around the cue *plus* the measured offset.
    assert result.windows[0].start == pytest.approx(
        profane[0].start + result.offset_s - result.window_pad_s, abs=0.1
    )


def test_a_discarded_track_keeps_its_cues_but_loses_its_timing(deploy, tmp_path):
    """Redaction is text-local and correct however bad the sync is."""
    words = [TranscriptWord(word=f"zzz{i}", start=100.0 + i, end=100.5 + i) for i in range(40)]
    ctx = make_ctx(
        deploy,
        tmp_path,
        transcript=Transcript(segments=[TranscriptSegment(start=100.0, end=140.0, words=words)]),
    )
    profane = cues()
    profane[0] = profane[0].model_copy(update={"text": CUE_TEXTS[0] + " shit"})

    result = _subs_after(ctx, profane)
    assert not result.usable
    assert result.windows == []
    assert result.hits == []
    assert len(result.cues) == 3, "the cues are the evidence for the verdict"
