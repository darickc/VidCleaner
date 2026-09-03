"""§6's audit comparison (`pipeline/audit.py`).

Pure, and deliberately so: this is the tier that proves the audit is *correct* without
a 30-minute speech-to-text run. The one thing it must never get wrong is the direction
of the merge -- an audit that replaced the mute set instead of extending it would make
files worse, and PLAN.md's own M2 numbers say by how much (windowed 49 detections, full
40, 22 of them missing from the full pass).
"""

from __future__ import annotations

from vidcleaner.pipeline.artifacts import Detection
from vidcleaner.pipeline.audit import AuditOptions, compare, covers, merged_result
from vidcleaner.pipeline.detect import DetectOptions


def det(
    word: str = "fuck",
    start: float = 10.0,
    end: float = 10.3,
    *,
    source: str = "stt",
    pad: float = 0.1,
    **kw,
) -> Detection:
    """A *finished* detection: both sides of the comparison come from `finalize`."""
    return Detection(
        word_raw=word,
        word_canonical=word,
        category=kw.pop("category", "strong"),
        start_s=start,
        end_s=end,
        mute_start_s=kw.pop("mute_start_s", max(0.0, start - pad)),
        mute_end_s=kw.pop("mute_end_s", end + pad),
        source=source,  # type: ignore[arg-type]
        **kw,
    )


# ----------------------------------------------------------------------- covers


def test_an_exact_repeat_is_covered() -> None:
    assert covers(det(), det()) is True


def test_a_small_offset_is_still_covered() -> None:
    """M2 measured cross-mode drift at median +0.009 s, so the common case is tiny."""
    assert covers(det(start=10.0, end=10.3), det(start=10.05, end=10.35)) is True


def test_the_slack_reaches_m2s_worst_observed_drift() -> None:
    """Max observed was 0.74 s. Below that the same word gets reported twice."""
    assert covers(det(start=10.0, end=10.3), det(start=10.74, end=11.04)) is True


def test_a_distant_hit_of_the_same_word_is_not_covered() -> None:
    """Two genuine `fuck`s four seconds apart are two findings, not one."""
    assert covers(det(start=10.0, end=10.3), det(start=14.0, end=14.3)) is False


def test_a_different_word_inside_the_same_span_is_not_covered() -> None:
    """Overlap alone would let a wide prior mute swallow a different word beside it."""
    assert covers(det("fuck", 10.0, 12.0), det("shit", 10.5, 10.8)) is False


def test_a_bare_god_inside_a_located_god_damn_is_covered() -> None:
    """`same_finding`'s phrase rule, which the detector already relies on."""
    assert covers(det("god damn", 10.0, 10.9), det("god", 10.1, 10.4)) is True


def test_the_prior_padded_span_is_what_counts() -> None:
    """If the previous run already mutes that audio, there is nothing new to hear --
    whatever the two passes think the word's exact boundaries were."""
    prior = det(start=10.0, end=10.3, mute_start_s=9.0, mute_end_s=11.0)
    assert covers(prior, det(start=10.8, end=10.95)) is True


# ---------------------------------------------------------------------- compare


def test_an_unheard_word_is_new() -> None:
    result = compare([det("fuck", 10.0, 10.3)], [det("shit", 40.0, 40.4)])
    assert [d.word_canonical for d in result.new] == ["shit"]
    assert len(result.carried) == 1


def test_everything_already_muted_yields_nothing_new() -> None:
    """The common case: an audit that confirms the file is already right."""
    prior = [det("fuck", 10.0, 10.3), det("shit", 40.0, 40.4)]
    result = compare(prior, [det("fuck", 10.02, 10.31), det("shit", 40.1, 40.5)])
    assert result.new == ()
    assert result.should_render is False
    assert len(result.carried) == 2


def test_an_empty_prior_set_makes_everything_new() -> None:
    result = compare([], [det(), det("shit", 40.0, 40.4)])
    assert len(result.new) == 2
    assert result.should_render is True


def test_one_prior_detection_covers_only_one_found_detection() -> None:
    """Otherwise "Bull. Shit." collapses to a single finding -- the same defect the
    detector's own timing selector had to fix in M1."""
    prior = [det("shit", 10.0, 10.3)]
    result = compare(prior, [det("shit", 10.05, 10.35), det("shit", 10.2, 10.5)])
    assert len(result.new) == 1


def test_a_whitelisted_prior_row_still_covers() -> None:
    """A context whitelist must not be undone by the audit re-finding the word."""
    prior = [det("god", 10.0, 10.3, muted=False, whitelisted=True)]
    result = compare(prior, [det("god", 10.05, 10.35)])
    assert result.new == ()


def test_an_unmuted_find_is_recorded_but_not_promotable() -> None:
    """A censored token with muting off changes no audio, so it earns no re-render."""
    result = compare([], [det(muted=False)])
    assert len(result.new) == 1
    assert result.promotable == ()
    assert result.should_render is False


def test_the_confidence_floor_gates_promotion_only() -> None:
    """M2: three of its 13 full-only hits scored under 0.01, "recognition noise"."""
    found = [det(confidence=0.005), det("shit", 40.0, 40.4, confidence=0.9)]
    result = compare([], found, opts=AuditOptions(min_confidence=0.1))
    assert len(result.new) == 2, "both are recorded"
    assert [d.word_canonical for d in result.promotable] == ["shit"]


def test_the_default_floor_promotes_everything_muted() -> None:
    """0.0 is deliberate: the threshold is unmeasured, so it must not drop findings."""
    result = compare([], [det(confidence=0.001)])
    assert len(result.promotable) == 1


def test_a_detection_with_no_confidence_is_promotable() -> None:
    result = compare([], [det(confidence=None)], opts=AuditOptions(min_confidence=0.5))
    assert len(result.promotable) == 1


# ------------------------------------------------------------------ improvements


def test_a_precise_hit_over_a_subtitle_fallback_is_an_improvement() -> None:
    """M2 measured subtitle-only fallbacks at +0.551 s error over 1.2-1.9 s spans,
    and said flatly that it is the fallback that is wrong."""
    prior = [det("fuck", 10.0, 11.5, source="subtitle", suspicious=True)]
    result = compare(prior, [det("fuck", 10.6, 10.9, source="stt")])
    assert result.new == ()
    assert len(result.improved) == 1
    assert result.improved[0][1].end_s - result.improved[0][1].start_s < 0.5


def test_an_improvement_alone_does_not_earn_a_re_render() -> None:
    """A swap, an arr rescan and a Jellyfin refresh for 200 ms of precision is a bad
    trade; §6's trigger is new *hits*."""
    prior = [det("fuck", 10.0, 11.5, source="subtitle")]
    result = compare(prior, [det("fuck", 10.6, 10.9)])
    assert result.improved
    assert result.should_render is False


def test_two_stt_passes_disagreeing_slightly_is_not_an_improvement() -> None:
    """That is noise, not precision."""
    prior = [det("fuck", 10.0, 10.4, source="stt")]
    result = compare(prior, [det("fuck", 10.05, 10.3, source="stt")])
    assert result.improved == ()
    assert len(result.carried) == 1


def test_a_wider_hit_never_replaces_a_narrower_one() -> None:
    prior = [det("fuck", 10.0, 10.3, source="subtitle")]
    result = compare(prior, [det("fuck", 9.8, 11.0)])
    assert result.improved == ()


# ----------------------------------------------------------------- merged_result


def test_the_merge_keeps_the_prior_mutes_and_adds_the_new_one() -> None:
    """The single most important assertion in the file. Without the union, 22 of the
    M2 episode's 49 muted words would have become audible again."""
    prior = [det("fuck", 10.0, 10.3), det("shit", 40.0, 40.4)]
    result = compare(prior, [det("bitch", 70.0, 70.5)])
    merged = merged_result(result, profile_hash="v1:abc", duration_s=100.0)

    words = sorted(d.word_canonical for d in merged.detections)
    assert words == ["bitch", "fuck", "shit"]
    assert merged.profile_hash == "v1:abc"
    assert len(merged.mute_ranges) == 3


def test_carried_detections_are_not_padded_a_second_time() -> None:
    """Both sides come from `finalize`, so re-padding would widen every mute by
    another pad_pre+pad_post on each audit -- swallowing the dialogue around it."""
    prior = [det("fuck", 10.0, 10.3, mute_start_s=9.92, mute_end_s=10.42)]
    merged = merged_result(compare(prior, []), duration_s=100.0)

    (kept,) = merged.detections
    assert kept.mute_start_s == 9.92
    assert kept.mute_end_s == 10.42


def test_adjacent_findings_from_the_two_passes_merge_into_one_range() -> None:
    """The reason the merge goes through `finalize` at all: `merge_ranges` has to run
    once over the union, or render would receive two overlapping ranges."""
    prior = [det("fuck", 10.0, 10.3, mute_start_s=9.9, mute_end_s=10.4)]
    found = [det("shit", 10.5, 10.8, mute_start_s=10.45, mute_end_s=10.9)]
    merged = merged_result(compare(prior, found), duration_s=100.0)

    assert len(merged.detections) == 2
    assert len(merged.mute_ranges) == 1, "250 ms default merge gap closes the join"


def test_an_improvement_replaces_the_prior_span_in_the_merge() -> None:
    prior = [det("fuck", 10.0, 11.5, source="subtitle", suspicious=True)]
    found = [det("fuck", 10.6, 10.9, source="stt")]
    merged = merged_result(compare(prior, found), duration_s=100.0)

    (only,) = merged.detections
    assert only.source == "stt"
    assert only.start_s == 10.6


def test_the_merge_recomputes_counts_and_totals(  # noqa: D103
) -> None:
    prior = [det("fuck", 10.0, 10.3)]
    merged = merged_result(
        compare(prior, [det("fuck", 40.0, 40.3)]),
        duration_s=100.0,
        detect_opts=DetectOptions(),
    )
    counts = {c.word_canonical: c.total for c in merged.counts}
    assert counts == {"fuck": 2}
    assert merged.stats["detections"] == 2
    assert merged.total_muted_s > 0
