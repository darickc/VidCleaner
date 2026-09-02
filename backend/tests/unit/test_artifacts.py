"""Typed artifact round-tripping, ranges, and the DB-mirror invariant."""

from __future__ import annotations

import pytest

from vidcleaner.pipeline.artifacts import (
    BITMAP_SUBTITLE_CODECS,
    TEXT_SUBTITLE_CODECS,
    ArtifactError,
    AudioStreamInfo,
    Check,
    CodecPlan,
    Detection,
    DetectionResult,
    JobSpec,
    ProbeResult,
    ProfileSnapshot,
    SubtitleStreamInfo,
    TimeRange,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    VerifyResult,
    merge_ranges,
    total_duration,
)


def probe(**kw) -> ProbeResult:
    defaults = dict(
        path="/media/x.mkv",
        size=100,
        mtime=1.0,
        clean_codec=CodecPlan(encoder="eac3", bit_rate=640000, reason="eac3_passthrough"),
    )
    return ProbeResult(**{**defaults, **kw})


# ---------------------------------------------------------------------- ranges


def test_duration_and_midpoint():
    r = TimeRange(start=1.0, end=2.5)
    assert r.duration == 1.5
    assert r.midpoint() == 1.75


def test_inverted_range_has_zero_duration():
    assert TimeRange(start=5.0, end=1.0).duration == 0.0


def test_padded_applies_both_sides():
    r = TimeRange(start=10.0, end=10.5).padded(0.08, 0.12)
    assert (round(r.start, 3), round(r.end, 3)) == (9.92, 10.62)


def test_padded_clamps_at_floor():
    r = TimeRange(start=0.03, end=0.2).padded(0.08, 0.12)
    assert r.start == 0.0 and round(r.end, 3) == 0.32


def test_padded_clamps_at_ceiling():
    r = TimeRange(start=9.9, end=10.0).padded(0.08, 0.5, ceil=10.1)
    assert round(r.end, 3) == 10.1


def test_overlaps():
    a = TimeRange(start=0.0, end=1.0)
    assert a.overlaps(TimeRange(start=0.5, end=1.5))
    assert not a.overlaps(TimeRange(start=1.0, end=2.0))


def test_merge_sorts_and_coalesces_overlaps():
    merged = merge_ranges(
        [
            TimeRange(start=3.0, end=4.0),
            TimeRange(start=1.0, end=2.0),
            TimeRange(start=1.5, end=2.5),
        ]
    )
    assert [(r.start, r.end) for r in merged] == [(1.0, 2.5), (3.0, 4.0)]


def test_merge_within_the_gap():
    merged = merge_ranges([TimeRange(start=9.0, end=10.0), TimeRange(start=10.2, end=11.0)], 0.25)
    assert [(r.start, r.end) for r in merged] == [(9.0, 11.0)]


def test_beyond_the_gap_stays_separate():
    merged = merge_ranges([TimeRange(start=9.0, end=10.0), TimeRange(start=10.3, end=11.0)], 0.25)
    assert len(merged) == 2


def test_merge_is_transitive():
    merged = merge_ranges(
        [
            TimeRange(start=0.0, end=1.0),
            TimeRange(start=1.2, end=2.0),
            TimeRange(start=2.2, end=3.0),
        ],
        0.25,
    )
    assert [(r.start, r.end) for r in merged] == [(0.0, 3.0)]


def test_merge_drops_zero_length_ranges():
    assert merge_ranges([TimeRange(start=1.0, end=1.0)]) == []


def test_merge_of_nothing_is_empty():
    assert merge_ranges([]) == []


def test_merged_output_is_sorted_and_disjoint():
    merged = merge_ranges(
        [TimeRange(start=s, end=s + 0.1) for s in (5.0, 1.0, 3.0, 9.0, 7.0)], 0.05
    )
    starts = [r.start for r in merged]
    assert starts == sorted(starts)
    assert all(a.end < b.start for a, b in zip(merged, merged[1:], strict=False))


def test_total_duration():
    assert total_duration([TimeRange(start=0, end=1), TimeRange(start=2, end=2.5)]) == 1.5


# ------------------------------------------------------------------ round trip


def test_probe_round_trips(tmp_path):
    original = probe(
        duration=100.0,
        audio=[AudioStreamInfo(index=1, typed_index=0, codec_name="eac3", channels=6)],
    )
    path = original.write(tmp_path / "probe.json")
    assert ProbeResult.read(path) == original


def test_transcript_round_trips_and_iterates_words(tmp_path):
    t = Transcript(
        model="large-v3-turbo",
        segments=[
            TranscriptSegment(
                start=0.0,
                end=1.0,
                words=[
                    TranscriptWord(word="you", start=0.0, end=0.3),
                    TranscriptWord(word="fucking", start=0.4, end=0.9, aligned=True),
                ],
            )
        ],
    )
    back = Transcript.read(t.write(tmp_path / "transcript.json"))
    assert back.word_count == 2
    assert [w.word for w in back.words] == ["you", "fucking"]


def test_read_rejects_a_missing_file(tmp_path):
    with pytest.raises(ArtifactError, match="cannot read"):
        ProbeResult.read(tmp_path / "nope.json")


def test_read_rejects_malformed_json(tmp_path):
    path = tmp_path / "probe.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactError, match="cannot parse"):
        ProbeResult.read(path)


def test_read_rejects_a_future_schema_version(tmp_path):
    path = tmp_path / "probe.json"
    payload = probe().model_dump()
    payload["schema_version"] = 99
    import json

    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="schema_version"):
        ProbeResult.read(path)


def test_unknown_fields_are_ignored_so_newer_artifacts_still_load(tmp_path):
    import json

    path = tmp_path / "probe.json"
    payload = probe().model_dump()
    payload["something_from_the_future"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert ProbeResult.read(path).path == "/media/x.mkv"


def test_write_is_atomic(tmp_path):
    probe().write(tmp_path / "probe.json")
    assert not list(tmp_path.glob(".*.tmp"))


# ---------------------------------------------------------------------- probe


def test_source_audio_resolves_by_typed_index():
    p = probe(
        source_audio_typed_index=1,
        audio=[
            AudioStreamInfo(index=1, typed_index=0, codec_name="ac3"),
            AudioStreamInfo(index=2, typed_index=1, codec_name="aac"),
        ],
    )
    assert p.source_audio.codec_name == "aac"


def test_source_audio_raises_when_absent():
    with pytest.raises(ArtifactError, match="is absent"):
        _ = probe(source_audio_typed_index=3).source_audio


def test_text_subtitles_filters_bitmap():
    p = probe(
        subtitles=[
            SubtitleStreamInfo(index=2, typed_index=0, codec_name="subrip"),
            SubtitleStreamInfo(index=3, typed_index=1, codec_name="hdmv_pgs_subtitle"),
        ]
    )
    assert [s.typed_index for s in p.text_subtitles] == [0]


@pytest.mark.parametrize("codec", sorted(TEXT_SUBTITLE_CODECS))
def test_text_codecs_report_as_text(codec):
    s = SubtitleStreamInfo(index=0, typed_index=0, codec_name=codec)
    assert s.is_text and not s.is_bitmap


@pytest.mark.parametrize("codec", sorted(BITMAP_SUBTITLE_CODECS))
def test_bitmap_codecs_report_as_bitmap(codec):
    s = SubtitleStreamInfo(index=0, typed_index=0, codec_name=codec)
    assert s.is_bitmap and not s.is_text


def test_codec_plan_lossless_flag():
    assert CodecPlan(encoder="flac", reason="x").is_lossless
    assert not CodecPlan(encoder="aac", bit_rate=160000, reason="x").is_lossless


# ----------------------------------------------------------------- detections


def detection(**kw) -> Detection:
    defaults = dict(
        word_raw="Fuck",
        word_canonical="fuck",
        category="strong",
        start_s=1.0,
        end_s=1.4,
        mute_start_s=0.92,
        mute_end_s=1.52,
        source="both",
    )
    return Detection(**{**defaults, **kw})


def test_detection_mirrors_the_database_columns():
    """persist.py is a mechanical copy, so drift here breaks M3 silently."""
    from vidcleaner.db.models import Detection as Row

    columns = {c.name for c in Row.__table__.columns}
    expected = columns - {"id", "job_id", "media_item_id"}
    fields = set(Detection.model_fields) - {"suspicious_reason"}
    assert fields == expected


def test_detection_mute_range():
    r = detection().mute_range
    assert (r.start, r.end) == (0.92, 1.52)


def test_detection_result_filters():
    result = DetectionResult(
        detections=[
            detection(),
            detection(whitelisted=True, muted=False),
            detection(suspicious=True),
        ]
    )
    assert len(result.muted) == 2
    assert len(result.suspicious) == 1


def test_detection_result_round_trips(tmp_path):
    original = DetectionResult(
        profile_hash="v1:abc",
        detections=[detection()],
        mute_ranges=[TimeRange(start=0.92, end=1.52)],
        total_muted_s=0.6,
        stats={"both": 1},
    )
    assert DetectionResult.read(original.write(tmp_path / "d.json")) == original


# --------------------------------------------------------------------- verify


def test_verify_separates_fatal_from_warn():
    result = VerifyResult(
        checks=[
            Check(name="a", ok=True),
            Check(name="b", ok=False, severity="fatal"),
            Check(name="c", ok=False, severity="warn"),
        ]
    )
    assert [c.name for c in result.failures] == ["b"]
    assert [c.name for c in result.warnings] == ["c"]


# ------------------------------------------------------------------- job spec


def test_job_spec_exposes_the_profile_hash():
    spec = JobSpec(
        job_id="j",
        version="0.1.0",
        source_path="/x.mkv",
        profile=ProfileSnapshot(profile_hash="v1:deadbeef"),
    )
    assert spec.profile_hash == "v1:deadbeef"
