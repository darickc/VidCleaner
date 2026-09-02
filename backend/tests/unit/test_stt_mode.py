"""The effective-STT-mode decision (PLAN.md §6 step 4, §13).

Before ``resolve_mode`` existed the mode was decided twice -- ``stt.run`` from
``spec.stt_mode`` and ``detect.run`` from "are there any cues" -- so the two
stages could disagree about the same job. These tests pin the single rule and
the two consequences that made the duplication a bug rather than a smell.
"""

from __future__ import annotations

import pytest

from vidcleaner.pipeline.artifacts import SubtitleCue, SubtitlesResult, TimeRange
from vidcleaner.pipeline.stages import deterministic_job_id
from vidcleaner.pipeline.stt import TranscribeRequest, resolve_mode
from vidcleaner.settings_store import AppSettings


def subs(*, cues: int = 2, windows: int = 1, usable: bool = True) -> SubtitlesResult:
    return SubtitlesResult(
        cues=[
            SubtitleCue(index=i, start=float(i), end=float(i) + 1.0, text="a word here")
            for i in range(cues)
        ],
        windows=[TimeRange(start=0.0, end=5.0) for _ in range(windows)],
        usable=usable,
    )


# ------------------------------------------------------------- the truth table


def test_subtitles_with_windows_stay_windowed():
    mode, reason = resolve_mode("windowed", subs(), duration_s=3600.0, settings=AppSettings())
    assert (mode, reason) == ("windowed", "subtitles")


@pytest.mark.parametrize(
    ("result", "expected_reason"),
    [
        (subs(cues=0, windows=0), "no_subtitles"),
        (subs(usable=False), "subtitles_unusable"),
    ],
)
def test_unusable_subtitles_promote_to_full(result, expected_reason):
    mode, reason = resolve_mode("windowed", result, duration_s=3600.0, settings=AppSettings())
    assert (mode, reason) == ("full", expected_reason)


def test_subtitles_with_no_hits_do_not_promote():
    """The expensive-mistake test: a clean episode must not trigger a full pass.

    Cues that parsed and matched nothing are evidence the file is clean, not
    missing information. Promoting here would put a full-file transcription on
    every clean episode in the library.
    """
    mode, reason = resolve_mode(
        "windowed", subs(cues=5, windows=0), duration_s=3600.0, settings=AppSettings()
    )
    assert (mode, reason) == ("windowed", "no_candidate_windows")


@pytest.mark.parametrize("requested", ["full", "audit"])
def test_an_explicit_mode_wins(requested):
    """Including over subtitles that would otherwise have kept the job windowed."""
    mode, reason = resolve_mode(requested, subs(), duration_s=3600.0, settings=AppSettings())
    assert (mode, reason) == (requested, "explicit")


# --------------------------------------------------------------- the hour cap


def test_promotion_is_refused_above_the_cap():
    settings = AppSettings(stt_full_max_hours=2.0)
    mode, reason = resolve_mode(
        "windowed", subs(cues=0, windows=0), duration_s=3.0 * 3600, settings=settings
    )
    assert (mode, reason) == ("windowed", "full_skipped_too_long")


def test_promotion_is_allowed_below_the_cap():
    settings = AppSettings(stt_full_max_hours=2.0)
    mode, _ = resolve_mode(
        "windowed", subs(cues=0, windows=0), duration_s=1.5 * 3600, settings=settings
    )
    assert mode == "full"


def test_a_zero_cap_means_no_limit():
    settings = AppSettings(stt_full_max_hours=0)
    mode, _ = resolve_mode(
        "windowed", subs(cues=0, windows=0), duration_s=99.0 * 3600, settings=settings
    )
    assert mode == "full"


def test_the_cap_never_overrules_an_explicit_request():
    """The cap stops the pipeline volunteering for a long pass, not the user."""
    settings = AppSettings(stt_full_max_hours=0.5)
    mode, reason = resolve_mode("full", subs(), duration_s=9.0 * 3600, settings=settings)
    assert (mode, reason) == ("full", "explicit")


# ------------------------------------------------------- progress denominator


def test_windowed_progress_uses_the_window_total():
    request = TranscribeRequest(
        audio_path="a.wav", windows=[TimeRange(start=0.0, end=30.0)], duration_s=3600.0
    )
    assert request.progress_total_s == 30.0


def test_full_progress_falls_back_to_the_duration():
    """Without this a full pass reports no progress at all: the window total is 0."""
    request = TranscribeRequest(audio_path="a.wav", windows=[], duration_s=3600.0)
    assert request.progress_total_s == 3600.0


def test_progress_total_is_none_when_nothing_is_known():
    assert TranscribeRequest(audio_path="a.wav").progress_total_s is None


# ------------------------------------------------------------------- resume


def test_a_non_default_mode_gets_its_own_work_dir(tmp_path):
    """Otherwise --stt-mode full resumes onto the windowed transcript and no-ops."""
    source = tmp_path / "movie.mkv"
    source.touch()
    windowed = deterministic_job_id(source, "v1:abc")
    assert deterministic_job_id(source, "v1:abc", stt_mode="full") != windowed
    assert deterministic_job_id(source, "v1:abc", stt_mode="audit") != windowed


def test_the_windowed_job_id_is_unchanged(tmp_path):
    """Existing work dirs must keep resuming after this change."""
    source = tmp_path / "movie.mkv"
    source.touch()
    assert deterministic_job_id(source, "v1:abc", stt_mode="windowed") == deterministic_job_id(
        source, "v1:abc"
    )


# ------------------------------------- the two stages must agree, end to end


@pytest.fixture
def deploy(tmp_path):
    from vidcleaner.config import Settings

    return Settings(config_dir=tmp_path / "config", work_dir=tmp_path / "work")


def _ctx(deploy, tmp_path, *, stt_mode="windowed", settings=None):
    from vidcleaner.matching.compiler import build_matcher
    from vidcleaner.pipeline.artifacts import (
        AudioStreamInfo,
        CodecPlan,
        ProbeResult,
        ProfileSnapshot,
    )
    from vidcleaner.pipeline.stages import build_context, build_spec

    source = tmp_path / "x.mkv"
    source.write_bytes(b"")
    spec = build_spec(
        source,
        profile=ProfileSnapshot(profile_hash="v1:x"),
        settings=settings or AppSettings(),
        stt_mode=stt_mode,
    )
    ctx = build_context(spec, deploy=deploy, runner=object())
    ctx.matcher = build_matcher()
    ProbeResult(
        path=str(source),
        size=1,
        mtime=0.0,
        duration=3600.0,
        audio=[AudioStreamInfo(index=0, typed_index=0, codec_name="ac3")],
        clean_codec=CodecPlan(encoder="ac3", bit_rate=640000),
    ).write(ctx.ws.probe_json)
    return ctx


def test_the_stage_records_the_mode_and_the_reason(deploy, tmp_path):
    """`detect` reads these back, so they are the contract between the stages."""
    from vidcleaner.pipeline import stt as stt_stage
    from vidcleaner.pipeline.artifacts import Transcript

    ctx = _ctx(deploy, tmp_path)
    subs(cues=0, windows=0).write(ctx.ws.subs_json)

    class Recorder:
        name = "recorder"

        def transcribe(self, request, on_progress=None):
            assert request.windows == [], "a full pass must not be given windows"
            assert request.duration_s == 3600.0
            return Transcript(mode=request.mode, model=request.model)

    ctx.transcriber = Recorder()
    stt_stage.run(ctx)

    result = stt_stage.load(ctx.ws)
    assert result.mode == "full"
    assert result.mode_reason == "no_subtitles"


def test_a_refused_promotion_says_so_in_the_transcript(deploy, tmp_path):
    """ "We declined to look" must be distinguishable from "this file is clean"."""
    from vidcleaner.pipeline import stt as stt_stage

    ctx = _ctx(deploy, tmp_path, settings=AppSettings(stt_full_max_hours=0.25))
    subs(cues=0, windows=0).write(ctx.ws.subs_json)

    stt_stage.run(ctx)
    result = stt_stage.load(ctx.ws)
    assert result.mode_reason == "full_skipped_too_long"
    assert result.word_count == 0


def test_detect_follows_the_transcript_not_the_cues(deploy, tmp_path):
    """A full transcript must be matched full-style even when cues exist.

    The consequence is concrete rather than stylistic. In windowed mode a
    subtitle hit that STT cannot corroborate still emits a 0.3-confidence
    ``source="subtitle"`` fallback, muting roughly 1.5 s around the cue. A
    full-file pass has *listened to that exact second and heard nothing there*,
    so honouring the cue anyway mutes dialogue on the strength of evidence the
    expensive pass just refuted. Deriving the mode from ``subs.cues`` -- as this
    did -- made ``--stt-mode full`` do precisely that.
    """
    from vidcleaner.pipeline import detect as detect_stage
    from vidcleaner.pipeline.artifacts import (
        SubtitleHit,
        Transcript,
        TranscriptSegment,
        TranscriptWord,
    )

    ctx = _ctx(deploy, tmp_path, stt_mode="full")
    result = subs(cues=1, windows=0)
    result = result.model_copy(
        update={
            "cues": [
                SubtitleCue(index=0, start=10.0, end=12.0, text="oh fuck that hurts"),
            ],
            "hits": [
                SubtitleHit(
                    cue_index=0,
                    start=10.3,
                    end=10.8,
                    word_raw="fuck",
                    word_canonical="fuck",
                    category="strong",
                    char_start=3,
                    char_end=7,
                )
            ],
        }
    )
    result.write(ctx.ws.subs_json)
    # The full pass heard a real word, far from the cue, and heard nothing at 10 s.
    Transcript(
        mode="full",
        mode_reason="explicit",
        segments=[
            TranscriptSegment(
                start=500.0,
                end=501.0,
                text="oh shit",
                words=[
                    TranscriptWord(word="oh", start=500.0, end=500.2),
                    TranscriptWord(word="shit", start=500.4, end=500.9),
                ],
            )
        ],
    ).write(ctx.ws.transcript_json)

    detect_stage.run(ctx)
    detections = detect_stage.load(ctx.ws).detections

    assert [d.word_canonical for d in detections] == ["shit"], (
        "a full pass must not emit the uncorroborated subtitle fallback at 10 s"
    )
    assert detections[0].source == "stt"
