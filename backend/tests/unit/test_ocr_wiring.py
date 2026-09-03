"""How OCR joins the pipeline (M6): precedence, scoping, and the no-mute rule.

None of this needs tesseract or ffmpeg -- it is the decision layer, and the
decisions are the part that can silently mute the wrong second of audio.
"""

from __future__ import annotations

from dataclasses import replace

from vidcleaner.matching.compiler import build_matcher
from vidcleaner.pipeline.artifacts import (
    CodecPlan,
    ProbeResult,
    SubtitleCue,
    SubtitleHit,
    SubtitleStreamInfo,
)
from vidcleaner.pipeline.detect import DetectOptions, detect
from vidcleaner.pipeline.subtitles import redactable_streams, subtitle_candidates


def probe_with(*streams: SubtitleStreamInfo) -> ProbeResult:
    return ProbeResult(
        path="/media/Show/S01E01.mkv",
        size=1000,
        mtime=1.0,
        duration=60.0,
        subtitles=list(streams),
        clean_codec=CodecPlan(encoder="ac3", bit_rate=192_000, channels=2, reason="test"),
    )


def pgs(typed_index: int = 0, language: str | None = "eng", forced: bool = False):
    return SubtitleStreamInfo(
        index=typed_index + 2,
        typed_index=typed_index,
        codec_name="hdmv_pgs_subtitle",
        language=language,
        is_forced=forced,
    )


def srt(typed_index: int = 0, language: str | None = "eng"):
    return SubtitleStreamInfo(
        index=typed_index + 2, typed_index=typed_index, codec_name="subrip", language=language
    )


# ------------------------------------------------------------------ precedence


def test_ocr_is_not_offered_unless_asked_for():
    assert subtitle_candidates(probe_with(pgs()), preferred_language="eng") == []


def test_a_pgs_stream_becomes_a_candidate_when_ocr_is_on():
    candidates = subtitle_candidates(probe_with(pgs()), preferred_language="eng", ocr=True)
    assert [c.kind for c in candidates] == ["ocr"]
    assert candidates[0].stream_typed_index == 0
    assert candidates[0].reason == "ocr_preferred_language"


def test_every_text_source_outranks_ocr():
    """A real text track is better evidence than anything Tesseract produces.

    Including a text track in the *wrong* language: it may still be usable, and
    the drift check is what decides that -- whereas OCR carries its own error
    rate on top.
    """
    candidates = subtitle_candidates(
        probe_with(srt(0, "fra"), pgs(1, "eng")), preferred_language="eng", ocr=True
    )
    assert [c.kind for c in candidates] == ["embedded", "ocr"]


def test_a_preferred_language_pgs_track_beats_a_foreign_one():
    candidates = subtitle_candidates(
        probe_with(pgs(0, "fra"), pgs(1, "eng")), preferred_language="eng", ocr=True
    )
    assert [c.stream_typed_index for c in candidates] == [1, 0]


def test_every_ocrable_stream_is_offered_exactly_once():
    candidates = subtitle_candidates(
        probe_with(pgs(0, "eng"), pgs(1, "eng", forced=True)), preferred_language="eng", ocr=True
    )
    assert sorted(c.stream_typed_index for c in candidates) == [0, 1]
    assert len(candidates) == 2


def test_vobsub_and_dvb_are_out_of_scope_and_stay_out():
    """PGS only. ffmpeg has no vobsub muxer, so extracting `.idx`/`.sub` would
    need mkvextract -- a whole extra binary for a format a Sonarr/Radarr library
    barely carries."""
    for codec in ("dvd_subtitle", "dvb_subtitle", "xsub"):
        stream = SubtitleStreamInfo(index=2, typed_index=0, codec_name=codec, language="eng")
        assert subtitle_candidates(probe_with(stream), preferred_language="eng", ocr=True) == []


# --------------------------------------------------------------------- scoping


def test_a_pgs_stream_is_never_a_redaction_target():
    """The whole safety story in one assertion.

    ``render`` only substitutes streams named in ``redactable``, and ``verify``
    fails the job if a bitmap stream changed codec. Keeping PGS out of this list
    is what makes "windowing only" structural rather than a promise.
    """
    probe = probe_with(pgs(0, "eng"))
    assert redactable_streams(probe) == []
    assert probe.text_subtitles == []
    assert [s.typed_index for s in probe.ocrable_subtitles] == [0]


# ------------------------------------------------------- the no-mute-alone rule


def detect_one(*, mute_subtitle_only: bool):
    """A subtitle hit with no STT token whatsoever -- the fallback case."""
    matcher = build_matcher()
    cues = [SubtitleCue(index=0, start=1.0, end=3.0, text="Oh shit, that hurt.")]
    hits = [
        SubtitleHit(
            cue_index=0,
            word_raw="shit",
            word_canonical="shit",
            category="strong",
            start=1.2,
            end=1.5,
            char_start=3,
            char_end=7,
        )
    ]
    return detect(
        matcher=matcher,
        cues=cues,
        tokens=[],
        mode="windowed",
        hits=hits,
        duration_s=60.0,
        opts=replace(DetectOptions(), mute_subtitle_only=mute_subtitle_only),
    )


def test_a_human_authored_cue_still_mutes_without_an_stt_token():
    """Unchanged behaviour for every pre-M6 source: §7's 0.3-confidence fallback."""
    result = detect_one(mute_subtitle_only=True)
    assert len(result.detections) == 1
    assert result.detections[0].muted is True
    assert result.mute_ranges, "a muted detection must produce a mute range"


def test_an_ocr_cue_with_no_stt_token_is_recorded_but_mutes_nothing():
    """M6's load-bearing rule.

    For a human cue "no STT token" means Whisper missed a word someone heard.
    For OCR it may equally mean OCR invented it -- and the fallback silences
    ~1.2 s of real dialogue on that guess. The row still exists so the Item page
    can show it; it simply does not silence anything.
    """
    result = detect_one(mute_subtitle_only=False)
    assert len(result.detections) == 1, "the detection is still recorded for review"
    detection = result.detections[0]
    assert detection.muted is False
    assert detection.suspicious is True
    assert "OCR" in (detection.suspicious_reason or "")
    assert result.mute_ranges == [], "nothing may be silenced on OCR evidence alone"


def test_the_rule_only_touches_the_fallback_not_confirmed_words():
    """An OCR cue that STT *does* confirm mutes normally -- the common case.

    Without this the feature would be pointless: it exists to find words, and a
    word both OCR and Whisper heard is exactly the one we came for.
    """
    from vidcleaner.pipeline.detect import Token

    matcher = build_matcher()
    cues = [SubtitleCue(index=0, start=1.0, end=3.0, text="Oh shit, that hurt.")]
    hits = [
        SubtitleHit(
            cue_index=0,
            word_raw="shit",
            word_canonical="shit",
            category="strong",
            start=1.2,
            end=1.5,
            char_start=3,
            char_end=7,
        )
    ]
    tokens = [
        Token(
            index=0,
            raw="shit",
            norm="shit",
            fold="shit",
            kind="word",
            start_s=1.25,
            end_s=1.45,
            prob=0.9,
        )
    ]
    result = detect(
        matcher=matcher,
        cues=cues,
        tokens=tokens,
        mode="windowed",
        hits=hits,
        duration_s=60.0,
        opts=replace(DetectOptions(), mute_subtitle_only=False),
    )
    assert result.detections[0].source == "both"
    assert result.detections[0].muted is True
    assert result.mute_ranges, "a word STT confirmed must still be muted"
