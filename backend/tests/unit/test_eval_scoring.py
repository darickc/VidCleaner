"""The eval harness's label schema and scoring (PLAN.md §12).

Runs with no media, no ffmpeg and no torch: the harness's arithmetic has to be
verifiable on a checkout that does not have the copyrighted episode, or it would
only ever be exercised on one machine.
"""

from __future__ import annotations

import pytest

from scripts.eval import (
    Clip,
    EvalError,
    Label,
    Metrics,
    load_all,
    load_label_set,
    score,
)


class FakeDetection:
    def __init__(self, word: str, start: float, end: float):
        self.word_canonical = word
        self.start_s = start
        self.end_s = end


class FakeRange:
    def __init__(self, start: float, end: float):
        self.start = start
        self.end = end


def clip(*labels, negatives=(), start=0.0, end=60.0) -> Clip:
    return Clip(id="c", start=start, end=end, labels=tuple(labels), negatives=tuple(negatives))


def label(word: str, start: float, end: float) -> Label:
    return Label(start=start, end=end, word=word)


# -------------------------------------------------------------------- scoring


def test_a_matching_detection_is_a_true_positive():
    metrics = score(clip(label("fuck", 10.0, 10.4)), [FakeDetection("fuck", 10.05, 10.45)])
    assert (metrics.true_positives, metrics.false_positives, metrics.false_negatives) == (1, 0, 0)
    assert metrics.precision == 1.0 and metrics.recall == 1.0


def test_a_missed_word_is_a_false_negative():
    metrics = score(clip(label("fuck", 10.0, 10.4)), [])
    assert metrics.false_negatives == 1
    assert metrics.recall == 0.0
    assert metrics.misses == ["fuck @ 10.00"]


def test_an_unlabelled_detection_is_a_false_positive():
    metrics = score(clip(), [FakeDetection("shit", 5.0, 5.3)])
    assert metrics.false_positives == 1
    assert metrics.precision == 0.0
    assert metrics.spurious == ["shit @ 5.00"]


def test_the_wrong_word_at_the_right_time_is_not_a_match():
    metrics = score(clip(label("fuck", 10.0, 10.4)), [FakeDetection("shit", 10.0, 10.4)])
    assert (metrics.true_positives, metrics.false_positives, metrics.false_negatives) == (0, 1, 1)


def test_the_right_word_at_the_wrong_time_is_not_a_match():
    metrics = score(clip(label("fuck", 10.0, 10.4)), [FakeDetection("fuck", 40.0, 40.4)])
    assert metrics.true_positives == 0


def test_two_detections_cannot_both_claim_one_label():
    """Otherwise a detector that fires twice per word scores perfect recall."""
    metrics = score(
        clip(label("fuck", 10.0, 10.4)),
        [FakeDetection("fuck", 10.0, 10.4), FakeDetection("fuck", 10.1, 10.5)],
    )
    assert (metrics.true_positives, metrics.false_positives) == (1, 1)


def test_the_nearest_detection_wins_the_pairing():
    metrics = score(
        clip(label("fuck", 10.0, 10.4)),
        [FakeDetection("fuck", 10.35, 10.75), FakeDetection("fuck", 10.02, 10.42)],
    )
    assert metrics.timing_errors == [pytest.approx(0.02)]


# ------------------------------------------------------------- mute coverage


def test_mute_coverage_is_one_when_the_word_is_fully_silenced():
    metrics = score(
        clip(label("fuck", 10.0, 10.4)),
        [FakeDetection("fuck", 10.0, 10.4)],
        [FakeRange(9.9, 10.6)],
    )
    assert metrics.mean_mute_coverage == 1.0


def test_a_detection_whose_mute_lands_short_is_still_a_miss_for_the_viewer():
    """The metric detection-level precision and recall cannot express: the word
    was found, and the viewer heard half of it anyway."""
    metrics = score(
        clip(label("fuck", 10.0, 10.4)),
        [FakeDetection("fuck", 10.0, 10.4)],
        [FakeRange(10.0, 10.2)],
    )
    assert metrics.true_positives == 1
    assert metrics.mean_mute_coverage == pytest.approx(0.5)


def test_a_missed_word_counts_as_zero_coverage():
    metrics = score(clip(label("fuck", 10.0, 10.4)), [], [])
    assert metrics.mean_mute_coverage == 0.0


# ---------------------------------------------------------------- negatives


def test_muting_a_must_not_mute_region_is_recorded():
    """The reverent "thank God" case §14 accepted as a known cost."""
    metrics = score(
        clip(negatives=[{"start": 20.0, "end": 21.0, "note": "thank God"}]),
        [],
        [FakeRange(20.2, 20.6)],
    )
    assert metrics.negatives_violated == 1
    assert "MUTED A NEGATIVE" in metrics.spurious[0]


def test_leaving_a_negative_alone_is_clean():
    metrics = score(clip(negatives=[{"start": 20.0, "end": 21.0}]), [], [FakeRange(30.0, 30.5)])
    assert metrics.negatives_violated == 0


def test_a_control_clip_with_no_labels_can_still_fail():
    """Its whole purpose: precision over labelled regions cannot catch this."""
    metrics = score(clip(), [FakeDetection("god", 12.0, 12.3)])
    assert metrics.precision == 0.0


# ------------------------------------------------------------------ merging


def test_metrics_merge_across_clips():
    a = score(clip(label("fuck", 10.0, 10.4)), [FakeDetection("fuck", 10.0, 10.4)])
    b = score(clip(label("shit", 20.0, 20.3)), [])
    total = a.merge(b)
    assert (total.true_positives, total.false_negatives) == (1, 1)
    assert total.recall == 0.5


def test_an_empty_metric_is_vacuously_perfect():
    """Nothing to find and nothing found is not a failure."""
    assert Metrics().precision == 1.0
    assert Metrics().recall == 1.0
    assert Metrics().median_timing_error is None


# ------------------------------------------------------------- label schema


def write(tmp_path, body: str):
    path = tmp_path / "set.yaml"
    path.write_text(body)
    return path


def test_a_label_outside_its_clip_is_rejected(tmp_path):
    """The likeliest authoring mistake: writing clip-relative times."""
    path = write(
        tmp_path,
        """
media: {name: t, file: t.mkv, duration_s: 100}
clips:
  - id: c1
    start: 50.0
    end: 60.0
    labels:
      - {start: 2.0, end: 2.4, word: fuck}
""",
    )
    with pytest.raises(EvalError, match="SOURCE time"):
        load_label_set(path)


def test_duplicate_clip_ids_are_rejected(tmp_path):
    path = write(
        tmp_path,
        """
media: {name: t, file: t.mkv, duration_s: 100}
clips:
  - {id: c1, start: 0, end: 10}
  - {id: c1, start: 20, end: 30}
""",
    )
    with pytest.raises(EvalError, match="duplicate"):
        load_label_set(path)


def test_a_backwards_clip_is_rejected(tmp_path):
    path = write(
        tmp_path,
        """
media: {name: t, file: t.mkv, duration_s: 100}
clips:
  - {id: c1, start: 30, end: 10}
""",
    )
    with pytest.raises(EvalError, match="ends before"):
        load_label_set(path)


def test_labels_are_sorted_by_time(tmp_path):
    path = write(
        tmp_path,
        """
media: {name: t, file: t.mkv, duration_s: 100}
clips:
  - id: c1
    start: 0
    end: 60
    labels:
      - {start: 40.0, end: 40.3, word: shit}
      - {start: 10.0, end: 10.3, word: fuck}
""",
    )
    assert [x.word for x in load_label_set(path).clips[0].labels] == ["fuck", "shit"]


# ------------------------------------------------- the committed label set


def test_the_committed_labels_parse():
    """They are checked in, so a typo must fail here rather than on the one
    machine that happens to hold the media."""
    sets = load_all()
    assert sets, "expected at least one committed label set"
    for label_set in sets:
        assert label_set.file, f"{label_set.name} names no media file"
        assert label_set.clips


def test_the_committed_set_has_a_control_clip():
    control = [c for s in load_all() for c in s.clips if not c.labels]
    assert control, "a set with no unlabelled clip cannot detect over-firing"


def test_unverified_labels_are_reported_as_such():
    """Timing error is withheld until a human has heard the words; this is the
    flag the harness reads to decide that."""
    for label_set in load_all():
        if any(not x.verified for x in label_set.all_labels):
            assert not label_set.verified
            break
    else:
        pytest.skip("every committed label has been verified")
