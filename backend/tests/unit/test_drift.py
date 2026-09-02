"""The subtitle drift measurement (PLAN.md §6 step 3).

Everything here is pure: no ffmpeg, no torch, no media. That is the point -- the
generated fixtures are sine tones, so the only way to test the arithmetic that
decides whether a library's subtitles can be trusted is to feed it constructed
words.
"""

from __future__ import annotations

import pytest
from rapidfuzz import fuzz

from vidcleaner.pipeline.artifacts import SubtitleCue, TranscriptWord
from vidcleaner.pipeline.drift import (
    MIN_ANCHOR_LEN,
    Observation,
    align_probe,
    build_result,
    cue_word_spans,
    decide,
    measure,
    plan_probes,
)


def cue(index: int, start: float, text: str, duration: float = 2.0) -> SubtitleCue:
    return SubtitleCue(index=index, start=start, end=start + duration, text=text)


def spoken(pairs) -> list[TranscriptWord]:
    return [TranscriptWord(word=w, start=t, end=t + 0.3) for w, t in pairs]


def shifted(plan, delta: float) -> list[TranscriptWord]:
    """What the cue's own words sound like when spoken ``delta`` seconds late."""
    return [
        TranscriptWord(word=w.fold, start=w.start + delta, end=w.end + delta) for w in plan.words
    ]


# ------------------------------------------------------------ cue word spans


def test_word_times_are_interpolated_across_the_cue():
    words = cue_word_spans(cue(0, 10.0, "alpha beta gamma delta", duration=4.0))
    assert [w.fold for w in words] == ["alpha", "beta", "gamma", "delta"]
    assert words[0].start == pytest.approx(10.0)
    # "delta" begins 17 chars into a 22-char cue spanning 4 s.
    assert words[-1].start == pytest.approx(10.0 + 4.0 * (17 / 22), abs=0.01)


def test_words_are_folded_so_punctuation_does_not_block_pairing():
    words = cue_word_spans(cue(0, 0.0, "Oh, God-damn it!"))
    assert [w.fold for w in words] == ["oh", "goddamn", "it"]


def test_punctuated_cue_words_still_pair_with_plain_speech():
    """Sentence-final words are exactly the ones whose timing matters most.

    ``fold`` alone leaves outer punctuation on, so a cue's "hurt." would not pair
    with a spoken "hurt" and would drop out of the alignment silently. The
    comparison key has to run the tokenizer's whole pipeline,
    ``strip_wrappers -> normalize -> fold``, on both sides.
    """
    plan = plan_probes([cue(0, 10.0, "Oh, God-damn it, that really hurt.")])[0]
    heard = spoken(
        [
            ("oh", 10.4),
            ("goddamn", 10.9),
            ("it", 11.4),
            ("that", 11.8),
            ("really", 12.2),
            ("hurt", 12.6),
        ]
    )
    observation = align_probe(plan, heard)
    assert observation.coverage == 1.0, "every cue word must pair, punctuation included"


def test_an_empty_cue_yields_no_words():
    assert cue_word_spans(cue(0, 0.0, "   ")) == []


# ------------------------------------------------------------------ probing


def test_probes_are_spread_across_the_file():
    cues = [cue(i, i * 10.0, "one two three four five six seven") for i in range(100)]
    plans = plan_probes(cues)
    assert len(plans) == 3
    indexes = [p.cue_index for p in plans]
    assert indexes == sorted(indexes)
    assert indexes[0] < 20 and 40 < indexes[1] < 60 and indexes[2] > 80


def test_probe_spans_are_padded_and_clamped():
    plans = plan_probes([cue(0, 1.0, "one two three four five six")], duration=4.0)
    assert plans[0].span.start == 0.0
    assert plans[0].span.end == 4.0


def test_short_cues_are_skipped_in_favour_of_long_ones():
    cues = [cue(0, 0.0, "hi"), cue(1, 10.0, "one two three four five six seven eight")]
    assert [p.cue_index for p in plan_probes(cues)] == [1]


def test_the_word_threshold_relaxes_rather_than_giving_up():
    """§6 offers no fallback; terse subtitles would otherwise never be checked."""
    cues = [cue(i, i * 10.0, "yes no maybe so") for i in range(6)]  # 4 words each
    assert plan_probes(cues), "should relax to the 4-word threshold"


def test_no_cues_means_no_probes():
    assert plan_probes([]) == []


# ------------------------------------------------------------------ pairing


def test_a_clean_offset_is_recovered():
    plan = plan_probes([cue(0, 10.0, "the quick brown fox jumps over")])[0]
    observation = align_probe(plan, shifted(plan, 0.9))
    assert observation.median == pytest.approx(0.9, abs=0.001)
    assert observation.coverage == 1.0


def test_repeated_words_pair_in_order():
    """The case nearest-time and greedy fuzzy matching both get wrong.

    "Bullshit. Bull. Shit." is in the project's own fixtures; a non-monotone
    pairing lets the second "shit" claim the first one's token and the measured
    offset becomes the gap between them.
    """
    plan = plan_probes([cue(0, 0.0, "bullshit bull shit right there now")])[0]
    observation = align_probe(plan, shifted(plan, 0.4))
    assert observation.median == pytest.approx(0.4, abs=0.001)
    assert observation.coverage == 1.0


def test_the_probe_pad_does_not_distort_the_measurement():
    """The ±5 s pad drags in speech either side; alignment must ignore it."""
    plan = plan_probes([cue(0, 20.0, "the quick brown fox jumps over")])[0]
    words = (
        spoken([("some", 15.0), ("earlier", 15.5), ("dialogue", 16.0)])
        + shifted(plan, 0.3)
        + spoken([("and", 26.0), ("later", 26.5), ("dialogue", 27.0)])
    )
    observation = align_probe(plan, words)
    assert observation.median == pytest.approx(0.3, abs=0.001)
    assert observation.coverage == 1.0


def test_short_words_are_not_used_as_anchors():
    """ "a"/"of"/"is" align by luck as often as by content, and are the commonest."""
    plan = plan_probes([cue(0, 0.0, "a an of to be or not")])[0]
    observation = align_probe(plan, shifted(plan, 0.5))
    assert all(len(w.fold) >= MIN_ANCHOR_LEN for w in plan.words if w.fold in {"not"})
    assert observation.coverage == 1.0, "every word still pairs"
    assert len(observation.deltas) == 1, "but only 'not' is long enough to anchor"


def test_unrelated_speech_gives_no_coverage():
    plan = plan_probes([cue(0, 0.0, "the quick brown fox jumps over")])[0]
    observation = align_probe(plan, spoken([("completely", 0.0), ("different", 1.0)]))
    assert observation.coverage == 0.0
    assert observation.median is None


def test_silence_gives_no_pairs():
    plan = plan_probes([cue(0, 0.0, "the quick brown fox jumps over")])[0]
    assert align_probe(plan, []).coverage == 0.0


# --------------------------------------------------------------- aggregation


def obs(cue_index: int, deltas, coverage: float = 1.0) -> Observation:
    return Observation(cue_index=cue_index, deltas=tuple(deltas), coverage=coverage, stt_text="")


def test_a_constant_offset_has_no_spread():
    offset, spread, coverage, probes = measure(
        [obs(0, [0.5, 0.52]), obs(1, [0.49, 0.51]), obs(2, [0.5, 0.5])]
    )
    assert offset == pytest.approx(0.5, abs=0.02)
    assert spread < 0.05
    assert (coverage, probes) == (1.0, 3)


def test_a_growing_offset_shows_up_as_spread():
    """Subtitles for another cut: no single correction fixes them."""
    _, spread, _, _ = measure([obs(0, [0.1]), obs(1, [1.4]), obs(2, [2.9])])
    assert spread == pytest.approx(2.8, abs=0.01)


def test_no_observations_measure_to_zero():
    assert measure([]) == (0.0, 0.0, 0.0, 0)


# ------------------------------------------------------------------ verdicts


def test_well_timed_subtitles_are_ok():
    assert decide(offset_s=0.05, spread_s=0.02, coverage=0.9, probes=3)[0] == "ok"


def test_a_large_offset_is_unreliable_not_discarded():
    """A measurable offset is correctable; widened windows beat a full pass."""
    action, reason = decide(offset_s=2.4, spread_s=0.1, coverage=0.9, probes=3)
    assert (action, reason) == ("unreliable", "offset_above_threshold")


def test_a_large_spread_is_unreliable():
    action, reason = decide(offset_s=0.1, spread_s=1.9, coverage=0.9, probes=3)
    assert (action, reason) == ("unreliable", "spread_above_threshold")


def test_low_coverage_discards_the_subtitles():
    """Wrong language or wrong episode: the cues do not describe this audio."""
    action, reason = decide(offset_s=0.0, spread_s=0.0, coverage=0.05, probes=3)
    assert (action, reason) == ("discard", "coverage_below_threshold")


def test_coverage_is_checked_before_offset():
    """An offset computed from two lucky pairings is not evidence of anything."""
    assert decide(offset_s=9.0, spread_s=0.0, coverage=0.1, probes=3)[0] == "discard"


def test_too_few_probes_is_skipped_not_a_verdict():
    action, reason = decide(offset_s=5.0, spread_s=0.0, coverage=0.0, probes=1)
    assert (action, reason) == ("skipped", "too_few_probes")


# ------------------------------------------------- why §6's metric is replaced


def test_plain_text_similarity_cannot_separate_match_from_mismatch():
    """PLAN.md §6 says "text similarity < 0.4 -> discard subs". It cannot work.

    The probe transcribes the cue plus a ±5 s pad, so the window text is several
    times longer than the cue and a whole-string ratio is dominated by the pad.
    A *perfect* match scores below §6's own threshold -- the rule as written
    would discard every subtitle track in the library.
    """
    cue_text = "oh my god damn that hurt"
    matching = (
        "i told you before we even got here that this was a bad idea and now look "
        "oh my god damn that hurt you should have listened to me when i said so"
    )
    unrelated = (
        "completely different dialogue from another episode entirely nothing in "
        "common with what the subtitle file happens to claim is being said"
    )
    assert fuzz.ratio(cue_text, matching) < 40.0
    assert fuzz.ratio(cue_text, matching) - fuzz.ratio(cue_text, unrelated) < 10.0


def test_coverage_separates_them_cleanly():
    plan = plan_probes([cue(0, 20.0, "oh my god damn that hurt")])[0]
    matching = align_probe(plan, shifted(plan, 0.1))
    unrelated = align_probe(
        plan, spoken([("completely", 18.0), ("different", 19.0), ("dialogue", 20.0)])
    )
    assert matching.coverage == 1.0
    assert unrelated.coverage == 0.0


# ------------------------------------------------------------------ artifact


def test_the_artifact_carries_the_evidence():
    plans = plan_probes([cue(0, 10.0, "the quick brown fox jumps over")])
    observations = [align_probe(plans[0], shifted(plans[0], 0.9))]
    result = build_result(observations, plans, model="small", elapsed_s=1.25)
    assert result.action == "skipped", "one probe is not a measurement"
    assert result.probes[0].cue_text == "the quick brown fox jumps over"
    assert result.probes[0].median_offset_s == pytest.approx(0.9, abs=0.001)
    assert result.model == "small"


def test_the_artifact_round_trips(tmp_path):
    from vidcleaner.pipeline.artifacts import DriftResult

    plans = plan_probes([cue(i, i * 100.0, "the quick brown fox jumps over") for i in range(3)])
    observations = [align_probe(p, shifted(p, 0.9)) for p in plans]
    result = build_result(observations, plans, model="small")
    assert result.action == "unreliable"

    path = tmp_path / "drift.json"
    result.write(path)
    assert DriftResult.read(path).offset_s == pytest.approx(0.9, abs=0.001)
