"""The detector: pairing subtitle hits with STT timing (PLAN.md §7).

Entirely pure -- hand-written cues and tokens in, DetectionResult out. Nothing
here imports torch, ffmpeg or the database.
"""

from __future__ import annotations

import pytest

from vidcleaner.matching.compiler import ProfileSpec, WhitelistRule, build_matcher
from vidcleaner.matching.normalize import tokenize
from vidcleaner.pipeline.artifacts import SubtitleCue
from vidcleaner.pipeline.detect import (
    CENSORED_CANONICAL,
    CONFIDENCE_CENSORED,
    CONFIDENCE_EXACT,
    CONFIDENCE_SUBTITLE_ONLY,
    DetectOptions,
    detect,
)
from vidcleaner.pipeline.subtitles import find_hits


@pytest.fixture
def matcher():
    return build_matcher()


def toks(matcher, *spec):
    """`("word", start, end)` triples -> normalized, timed tokens."""
    return [
        tokenize(w, index=i, never_match=matcher.never_match, start_s=s, end_s=e, prob=0.9)
        for i, (w, s, e) in enumerate(spec)
    ]


def cue(text: str, start: float = 10.0, end: float = 12.0, index: int = 0) -> SubtitleCue:
    return SubtitleCue(index=index, start=start, end=end, text=text)


def run(matcher, cues, tokens, **kw):
    cue_list = list(cues)
    return detect(
        matcher=matcher,
        cues=cue_list,
        tokens=tokens,
        hits=find_hits(cue_list, matcher),
        duration_s=kw.pop("duration_s", 100.0),
        opts=kw.pop("opts", DetectOptions()),
        **kw,
    )


# ------------------------------------------------------------------ pairing


def test_subtitle_hit_takes_timing_from_the_matching_token(matcher):
    c = cue("You fucking idiot", 10.0, 12.0)
    tokens = toks(matcher, ("You", 10.0, 10.3), ("fucking", 10.4, 10.9), ("idiot", 11.0, 11.4))
    result = run(matcher, [c], tokens)

    (hit,) = [d for d in result.detections if d.word_canonical == "fuck"]
    assert (hit.start_s, hit.end_s) == (10.4, 10.9)
    assert hit.source == "both"
    assert hit.confidence == CONFIDENCE_EXACT
    assert not hit.suspicious


def test_fuzzy_matching_compares_against_the_whole_form_table(matcher):
    """`fuzz.ratio("fuck", "fucking")` is 72.7 -- below the threshold on its own."""
    c = cue("What the fuck", 10.0, 12.0)
    tokens = toks(matcher, ("What", 10.0, 10.2), ("the", 10.3, 10.4), ("fucking", 10.5, 11.0))
    (hit,) = run(matcher, [c], tokens).detections
    assert (hit.start_s, hit.end_s) == (10.5, 11.0)
    assert hit.source == "both"


def test_an_unrelated_token_is_rejected(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("Oh", 10.0, 10.2), ("truck", 10.3, 10.8))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.source == "subtitle"
    assert hit.confidence == CONFIDENCE_SUBTITLE_ONLY
    assert hit.suspicious


def test_a_never_match_token_is_not_chosen_for_timing(matcher):
    """`fuzz.ratio("shit", "shirt")` is 88.9, over the threshold."""
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("Oh", 10.0, 10.2), ("shirt", 10.3, 10.8))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.source == "subtitle", "shirt must not supply the timing"


def test_the_correct_token_wins_over_a_near_miss(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("shirt", 10.1, 10.4), ("shit", 10.5, 10.9))
    (hit,) = run(matcher, [c], tokens).detections
    assert (hit.start_s, hit.end_s) == (10.5, 10.9)


def test_no_token_falls_back_to_the_proportional_span(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    result = run(matcher, [c], [])
    (hit,) = result.detections
    assert hit.source == "subtitle"
    assert hit.suspicious and hit.suspicious_reason
    # "shit" starts at char 3 of 7 -> ~43% into a 2 s cue, padded by 0.4 s
    assert hit.start_s == pytest.approx(10.0 + 2.0 * (3 / 7) - 0.4, abs=0.01)


def test_a_token_far_from_the_cue_midpoint_is_rejected(matcher):
    """The cue falls back to its own span; the distant token is reported separately.

    Whisper genuinely heard the word at 20 s, so suppressing that would lose a
    real detection -- it just must not be used as *this* cue's timing.
    """
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 20.0, 20.4))
    result = run(matcher, [c], tokens)

    (from_cue,) = [d for d in result.detections if d.subtitle_cue_idx == 0]
    assert from_cue.source == "subtitle"
    assert from_cue.start_s < 13.0
    (from_stt,) = [d for d in result.detections if d.source == "stt"]
    assert (from_stt.start_s, from_stt.end_s) == (20.0, 20.4)


# ----------------------------------------------------------------- censored


def test_a_censored_token_supplies_timing_for_a_subtitle_hit(matcher):
    """Ordering guard: fuzz.ratio("f***", "fuck") is ~50, so censored must run first."""
    c = cue("What the fuck", 10.0, 12.0)
    tokens = toks(matcher, ("What", 10.0, 10.2), ("f***", 10.5, 11.0))
    (hit,) = run(matcher, [c], tokens).detections
    assert (hit.start_s, hit.end_s) == (10.5, 11.0)
    assert hit.source == "both"
    assert hit.confidence == CONFIDENCE_CENSORED


def test_a_masked_token_without_subtitle_evidence_is_still_muted(matcher):
    """`f***` is ambiguous (fuck, fag, faggot), so it uses the sentinel canonical.

    §7 mutes it anyway at 0.6 confidence: the mask is unambiguous evidence that
    *something* was censored, even when the word itself is not recoverable.
    """
    c = cue("He was tired", 10.0, 12.0)
    tokens = toks(matcher, ("He", 10.0, 10.2), ("f***", 10.5, 11.0), ("tired", 11.1, 11.4))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.word_canonical == CENSORED_CANONICAL
    assert hit.category == "strong"
    assert hit.source == "stt"
    assert hit.suspicious and hit.muted


@pytest.mark.parametrize(("token", "expected"), [("f**k", "fuck"), ("b*tch", "bitch")])
def test_an_unambiguous_masked_token_names_the_word(matcher, token, expected):
    """Revealed letters narrow the candidates for free.

    `f***` stays ambiguous (fuck/fag/faggot) but `f**k` pins the final letter.
    `sh*t` is deliberately not used here: it legitimately matches `shite` too.
    """
    c = cue("He was tired", 10.0, 12.0)
    tokens = toks(matcher, (token, 10.5, 11.0))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.word_canonical == expected


def test_an_ambiguous_masked_token_uses_the_sentinel_canonical(matcher):
    c = cue("He was tired", 10.0, 12.0)
    tokens = toks(matcher, ("s***", 10.5, 11.0))
    hits = [d for d in run(matcher, [c], tokens).detections if d.source == "stt"]
    assert hits and hits[0].word_canonical in {CENSORED_CANONICAL, "shit", "slut"}


def test_mute_censored_tokens_false_records_but_does_not_mute(matcher):
    c = cue("He was tired", 10.0, 12.0)
    tokens = toks(matcher, ("f***", 10.5, 11.0))
    result = run(matcher, [c], tokens, opts=DetectOptions(mute_censored_tokens=False))
    (hit,) = result.detections
    assert hit.muted is False
    assert result.mute_ranges == []


def test_a_hyphen_token_alone_produces_no_detection(matcher):
    """`f-ing` is a shape heuristic; without subtitle evidence it must not fire."""
    c = cue("He was tired", 10.0, 12.0)
    tokens = toks(matcher, ("f-ing", 10.5, 11.0))
    assert run(matcher, [c], tokens).detections == []


# ------------------------------------------------------------------ phrases


def test_a_phrase_spans_its_first_and_last_token(matcher):
    c = cue("God damn it", 5.0, 6.0)
    tokens = toks(matcher, ("God", 5.0, 5.2), ("damn", 5.3, 5.6), ("it", 5.7, 5.9))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.word_canonical == "god damn"
    assert (hit.start_s, hit.end_s) == (5.0, 5.6)
    assert not hit.suspicious, "'it' must stay audible"


def test_focus_narrows_a_phrase_to_the_profane_word(matcher):
    """`son of a bitch` mutes only `bitch`, not 1.2 s of dialogue."""
    c = cue("You son of a bitch", 5.0, 7.0)
    tokens = toks(
        matcher,
        ("You", 5.0, 5.2),
        ("son", 5.3, 5.5),
        ("of", 5.6, 5.7),
        ("a", 5.8, 5.85),
        ("bitch", 5.9, 6.3),
    )
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.word_canonical == "son of a bitch"
    assert (hit.start_s, hit.end_s) == (5.9, 6.3)


def test_a_partially_located_phrase_is_suspicious(matcher):
    c = cue("God damn it", 5.0, 6.0)
    tokens = toks(matcher, ("God", 5.0, 5.2), ("mumble", 5.3, 5.6))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.suspicious and "phrase" in (hit.suspicious_reason or "")


# ---------------------------------------------------------------- stt-only


def test_a_word_the_subtitles_sanitised_is_still_detected(matcher):
    """Free: the window is already transcribed."""
    c = cue("You idiot", 10.0, 12.0)
    tokens = toks(matcher, ("You", 10.0, 10.2), ("fucking", 10.3, 10.8), ("idiot", 10.9, 11.2))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.word_canonical == "fuck"
    assert hit.source == "stt"


def test_stt_and_subtitle_hits_do_not_double_count(matcher):
    c = cue("You fucking idiot", 10.0, 12.0)
    tokens = toks(matcher, ("You", 10.0, 10.2), ("fucking", 10.3, 10.8), ("idiot", 10.9, 11.2))
    result = run(matcher, [c], tokens)
    assert len(result.detections) == 1
    assert result.detections[0].source == "both"


# ------------------------------------------------------------------- guards


def test_an_over_long_span_falls_back_and_is_flagged(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 10.1, 14.5))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.suspicious
    assert "exceeds" in (hit.suspicious_reason or "")
    assert hit.end_s - hit.start_s < 4.0


def test_guards_run_before_padding(matcher):
    """A 2.9 s span plus 200 ms of padding must NOT be flagged."""
    c = cue("Oh shit", 10.0, 14.0)
    tokens = toks(matcher, ("shit", 11.0, 13.9))
    (hit,) = run(matcher, [c], tokens).detections
    assert hit.end_s - hit.start_s == pytest.approx(2.9)
    assert not hit.suspicious
    assert hit.mute_end_s - hit.mute_start_s > 3.0, "padded span does exceed the guard"


def test_guard_thresholds_come_from_options(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 10.1, 12.1))
    lenient = run(matcher, [c], tokens, opts=DetectOptions(max_range_s=5.0))
    strict = run(matcher, [c], tokens, opts=DetectOptions(max_range_s=1.0))
    assert not lenient.detections[0].suspicious
    assert strict.detections[0].suspicious


# --------------------------------------------------------- padding & merging


def test_padding_is_applied_from_the_profile(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 10.5, 11.0))
    (hit,) = run(
        matcher, [c], tokens, opts=DetectOptions(pad_pre_ms=80, pad_post_ms=120)
    ).detections
    assert hit.mute_start_s == pytest.approx(10.42)
    assert hit.mute_end_s == pytest.approx(11.12)


def test_padding_is_clamped_at_zero_and_the_duration(matcher):
    c = cue("Oh shit", 0.0, 1.0)
    tokens = toks(matcher, ("shit", 0.01, 0.95))
    (hit,) = run(matcher, [c], tokens, duration_s=1.0).detections
    assert hit.mute_start_s == 0.0
    assert hit.mute_end_s <= 1.0


def test_adjacent_detections_merge_into_one_range(matcher):
    c = cue("Shit shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 10.0, 10.3), ("shit", 10.5, 10.8))
    result = run(matcher, [c], tokens, opts=DetectOptions(merge_gap_ms=250))
    assert len(result.detections) == 2
    assert len(result.mute_ranges) == 1


def test_distant_detections_stay_separate(matcher):
    cues = [cue("Shit", 10.0, 11.0, 0), cue("Shit", 40.0, 41.0, 1)]
    tokens = toks(matcher, ("shit", 10.1, 10.4), ("shit", 40.1, 40.4))
    result = run(matcher, cues, tokens)
    assert len(result.mute_ranges) == 2


def test_per_detection_ranges_stay_pre_merge(matcher):
    """§5's columns are per detection and M4's snippets need a per-word range."""
    c = cue("Shit shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 10.0, 10.3), ("shit", 10.5, 10.8))
    result = run(matcher, [c], tokens)
    assert len({(d.mute_start_s, d.mute_end_s) for d in result.detections}) == 2
    assert len(result.mute_ranges) == 1


def test_a_minimum_mute_length_is_enforced(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 10.5, 10.5))
    (hit,) = run(matcher, [c], tokens, opts=DetectOptions(pad_pre_ms=0, pad_post_ms=0)).detections
    assert hit.mute_end_s - hit.mute_start_s == pytest.approx(0.02, abs=1e-6)


# ---------------------------------------------------------------- whitelist


def test_a_whitelisted_word_is_recorded_but_not_muted():
    m = build_matcher(whitelist=[WhitelistRule("shit")])
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(m, ("shit", 10.5, 11.0))
    result = run(m, [c], tokens)
    # The subtitle stage would not have produced a hit, but STT-only still sees it
    assert result.mute_ranges == []


# ---------------------------------------------------------------- reporting


def test_counts_group_by_canonical_and_category(matcher):
    cues = [cue("Shit", 10.0, 11.0, 0), cue("Bullshit", 20.0, 21.0, 1), cue("Shit", 30.0, 31.0, 2)]
    tokens = toks(matcher, ("shit", 10.1, 10.4), ("bullshit", 20.1, 20.6), ("shit", 30.1, 30.4))
    counts = {c.word_canonical: c for c in run(matcher, cues, tokens).counts}
    assert counts["shit"].total == 2
    assert counts["bullshit"].total == 1
    assert counts["shit"].sources == {"both": 2}


def test_counts_are_ordered_by_frequency(matcher):
    cues = [cue("Shit", 10.0, 11.0, 0), cue("Shit", 20.0, 21.0, 1), cue("Bullshit", 30.0, 31.0, 2)]
    tokens = toks(matcher, ("shit", 10.1, 10.4), ("shit", 20.1, 20.4), ("bullshit", 30.1, 30.6))
    counts = run(matcher, cues, tokens).counts
    assert [c.total for c in counts] == sorted([c.total for c in counts], reverse=True)


def test_stats_and_totals_are_populated(matcher):
    c = cue("Oh shit", 10.0, 12.0)
    tokens = toks(matcher, ("shit", 10.5, 11.0))
    result = run(matcher, [c], tokens)
    assert result.stats["detections"] == 1
    assert result.stats["muted"] == 1
    assert result.total_muted_s > 0
    assert result.profile_hash == matcher.profile_hash


def test_detections_are_sorted_by_time(matcher):
    cues = [cue("Shit", 30.0, 31.0, 0), cue("Shit", 10.0, 11.0, 1)]
    tokens = toks(matcher, ("shit", 30.1, 30.4), ("shit", 10.1, 10.4))
    starts = [d.start_s for d in run(matcher, cues, tokens).detections]
    assert starts == sorted(starts)


def test_a_clean_file_yields_nothing(matcher):
    c = cue("Perfectly ordinary dialogue", 10.0, 12.0)
    tokens = toks(matcher, ("Perfectly", 10.0, 10.5), ("ordinary", 10.6, 11.0))
    result = run(matcher, [c], tokens)
    assert result.detections == [] and result.mute_ranges == []


def test_never_match_words_never_produce_detections(matcher):
    c = cue("Nothing to see in Scunthorpe", 10.0, 12.0)
    tokens = toks(matcher, ("Scunthorpe", 10.5, 11.2), ("classic", 11.3, 11.8))
    assert run(matcher, [c], tokens).detections == []


# ------------------------------------------------------------- full mode (M2)


def test_full_mode_matches_the_whole_transcript(matcher):
    tokens = toks(matcher, ("You", 1.0, 1.2), ("fucking", 1.3, 1.8), ("idiot", 1.9, 2.2))
    result = detect(matcher=matcher, cues=None, tokens=tokens, mode="full", duration_s=10.0)
    (hit,) = result.detections
    assert hit.word_canonical == "fuck"
    assert hit.source == "stt"
    assert hit.subtitle_cue_idx is None


def test_full_mode_spans_a_phrase_across_tokens(matcher):
    tokens = toks(matcher, ("oh", 1.0, 1.1), ("god", 1.2, 1.4), ("damn", 1.5, 1.8))
    result = detect(matcher=matcher, cues=None, tokens=tokens, mode="full", duration_s=10.0)
    (hit,) = result.detections
    assert hit.word_canonical == "god damn"
    assert (hit.start_s, hit.end_s) == (1.2, 1.8)


def test_full_and_windowed_agree_on_the_same_text(matcher):
    """The property the M2 audit pass depends on."""
    spec = (("You", 10.0, 10.2), ("fucking", 10.3, 10.8), ("idiot", 10.9, 11.2))
    tokens = toks(matcher, *spec)
    windowed = run(matcher, [cue("You fucking idiot", 10.0, 12.0)], tokens)
    full = detect(matcher=matcher, cues=None, tokens=tokens, mode="full", duration_s=100.0)
    assert [d.word_canonical for d in windowed.detections] == [
        d.word_canonical for d in full.detections
    ]
    assert windowed.detections[0].start_s == full.detections[0].start_s


def test_punctuation_only_tokens_do_not_break_the_mapping(matcher):
    tokens = toks(matcher, ("god", 1.0, 1.2), ("...", 1.25, 1.3), ("damn", 1.4, 1.7))
    result = detect(matcher=matcher, cues=None, tokens=tokens, mode="full", duration_s=10.0)
    assert [d.word_canonical for d in result.detections] == ["god"]


# --------------------------------------------------------------- profile opts


def test_options_are_read_from_a_profile():
    opts = DetectOptions.from_profile(
        ProfileSpec(pad_pre_ms=150, pad_post_ms=250, merge_gap_ms=500, mute_censored_tokens=False)
    )
    assert (opts.pad_pre_ms, opts.pad_post_ms, opts.merge_gap_ms) == (150, 250, 500)
    assert opts.mute_censored_tokens is False
