"""The pure half of verification: structural comparison and window choice."""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.pipeline.artifacts import (
    AudioStreamInfo,
    CodecPlan,
    DetectionResult,
    ProbeResult,
    SubtitleStreamInfo,
    TimeRange,
    VideoStreamInfo,
)
from vidcleaner.pipeline.render import plan_render
from vidcleaner.pipeline.verify import (
    WINDOW_INSET_S,
    pick_control_window,
    pick_probe_windows,
    structural_checks,
)

VIDEO = VideoStreamInfo(
    index=0, typed_index=0, codec_name="h264", width=1920, height=1080, pix_fmt="yuv420p"
)


def audio(typed_index: int, **kw) -> AudioStreamInfo:
    defaults = dict(
        index=typed_index + 1,
        typed_index=typed_index,
        codec_name="ac3",
        channels=6,
        language="eng",
    )
    return AudioStreamInfo(**{**defaults, **kw})


def source_probe(**kw) -> ProbeResult:
    defaults = dict(
        path="/media/in.mkv",
        size=1_000_000,
        mtime=1.0,
        duration=100.0,
        fingerprint="fp",
        video=[VIDEO],
        audio=[audio(0, is_default=True, dispositions=("default",))],
        subtitles=[SubtitleStreamInfo(index=2, typed_index=0, codec_name="subrip", language="eng")],
        chapter_count=2,
        clean_codec=CodecPlan(encoder="ac3", bit_rate=640_000, reason="ac3_passthrough"),
    )
    return ProbeResult(**{**defaults, **kw})


def output_probe(**kw) -> ProbeResult:
    defaults = dict(
        path="/work/out.mkv",
        size=1_100_000,
        mtime=2.0,
        duration=100.0,
        video=[VIDEO],
        audio=[
            audio(0, title="Clean", is_default=True, dispositions=("default",)),
            audio(1, title="Original", is_default=False, dispositions=()),
        ],
        subtitles=[SubtitleStreamInfo(index=3, typed_index=0, codec_name="subrip", language="eng")],
        chapter_count=2,
        clean_codec=CodecPlan(encoder="ac3", bit_rate=640_000, reason="ac3_passthrough"),
        tags={
            "VIDCLEANER": "1",
            "VIDCLEANER_VERSION": "0.1.0",
            "VIDCLEANER_PROFILE_HASH": "v1:h",
            "VIDCLEANER_JOB": "j",
            "VIDCLEANER_SRC_FP": "fp",
        },
    )
    return ProbeResult(**{**defaults, **kw})


def checks(src=None, out=None):
    src = src or source_probe()
    plan = plan_render(
        src, DetectionResult(), output=Path("/work/out.mkv"), job_id="j", profile_hash="v1:h"
    )
    return {c.name: c for c in structural_checks(src, out or output_probe(), plan)}


def test_a_good_render_passes_every_fatal_check():
    failed = [c for c in checks().values() if c.severity == "fatal" and not c.ok]
    assert failed == []


@pytest.mark.parametrize(
    ("name", "broken"),
    [
        ("audio_stream_count", {"audio": [audio(0, title="Clean", is_default=True)]}),
        (
            "clean_track_title",
            {"audio": [audio(0, title="Wrong", is_default=True), audio(1, title="Original")]},
        ),
        ("clean_track_default", {"audio": [audio(0, title="Clean"), audio(1, title="Original")]}),
        (
            "clean_track_channels",
            {
                "audio": [
                    audio(0, title="Clean", channels=2, is_default=True),
                    audio(1, title="Original"),
                ]
            },
        ),
        (
            "clean_track_language",
            {
                "audio": [
                    audio(0, title="Clean", language="fre", is_default=True),
                    audio(1, title="Original"),
                ]
            },
        ),
        (
            "single_default_audio",
            {
                "audio": [
                    audio(0, title="Clean", is_default=True),
                    audio(1, title="Original", is_default=True),
                ]
            },
        ),
        ("subtitle_stream_count", {"subtitles": []}),
        ("duration_within_tolerance", {"duration": 108.0}),
        ("output_size_floor", {"size": 100}),
        ("video_streams_identical", {"video": []}),
    ],
)
def test_each_fatal_failure_is_detected(name, broken):
    result = checks(out=output_probe(**broken))
    assert not result[name].ok, f"{name} should have failed"
    assert result[name].severity == "fatal"


def test_missing_idempotency_tags_are_fatal():
    result = checks(out=output_probe(tags={"VIDCLEANER": "1"}))
    assert not result["idempotency_tags"].ok


def test_a_mismatched_source_fingerprint_is_fatal():
    tags = dict(output_probe().tags)
    tags["VIDCLEANER_SRC_FP"] = "different"
    result = checks(out=output_probe(tags=tags))
    assert not result["source_fingerprint_tag"].ok


def test_losing_chapters_is_fatal():
    result = checks(out=output_probe(chapter_count=0))
    assert not result["chapters_preserved"].ok
    assert result["chapters_preserved"].severity == "fatal"


def test_a_bitmap_subtitle_codec_change_is_fatal():
    src = source_probe(
        subtitles=[SubtitleStreamInfo(index=2, typed_index=0, codec_name="hdmv_pgs_subtitle")]
    )
    out = output_probe(subtitles=[SubtitleStreamInfo(index=3, typed_index=0, codec_name="subrip")])
    result = checks(src, out)
    assert not result["bitmap_subtitles_untouched"].ok
    assert result["bitmap_subtitles_untouched"].severity == "fatal"


def test_the_planned_mov_text_transcode_is_only_a_warning():
    src = source_probe(
        subtitles=[
            SubtitleStreamInfo(index=2, typed_index=0, codec_name="mov_text", language="eng")
        ]
    )
    out = output_probe(
        subtitles=[SubtitleStreamInfo(index=3, typed_index=0, codec_name="subrip", language="eng")]
    )
    result = checks(src, out)
    assert result["subtitle_codecs"].ok


def test_an_unplanned_text_transcode_is_a_warning_not_a_failure():
    src = source_probe(subtitles=[SubtitleStreamInfo(index=2, typed_index=0, codec_name="subrip")])
    out = output_probe(subtitles=[SubtitleStreamInfo(index=3, typed_index=0, codec_name="ass")])
    result = checks(src, out)
    assert not result["subtitle_codecs"].ok
    assert result["subtitle_codecs"].severity == "warn"


def test_wiping_the_original_dispositions_is_a_warning():
    """The C4 regression check."""
    src = source_probe(
        audio=[audio(0, is_default=True, dispositions=("default", "original", "comment"))]
    )
    out = output_probe(
        audio=[
            audio(0, title="Clean", is_default=True, dispositions=("default",)),
            audio(1, title="Original", dispositions=()),
        ]
    )
    result = checks(src, out)
    assert not result["original_dispositions_preserved"].ok
    assert result["original_dispositions_preserved"].severity == "warn"


def test_preserved_dispositions_pass():
    src = source_probe(audio=[audio(0, is_default=True, dispositions=("default", "original"))])
    out = output_probe(
        audio=[
            audio(0, title="Clean", is_default=True, dispositions=("default",)),
            audio(1, title="Original", dispositions=("original",)),
        ]
    )
    assert checks(src, out)["original_dispositions_preserved"].ok


def test_an_absent_source_language_makes_the_check_a_warning():
    src = source_probe(audio=[audio(0, language=None, is_default=True)])
    out = output_probe(
        audio=[
            audio(0, title="Clean", language=None, is_default=True),
            audio(1, title="Original", language=None),
        ]
    )
    result = checks(src, out)
    assert result["clean_track_language"].ok
    assert result["clean_track_language"].severity == "warn"


def test_a_wrong_clean_codec_is_only_a_warning():
    out = output_probe(
        audio=[
            audio(0, title="Clean", codec_name="eac3", is_default=True),
            audio(1, title="Original"),
        ]
    )
    result = checks(out=out)
    assert not result["clean_track_codec"].ok
    assert result["clean_track_codec"].severity == "warn"


def test_every_check_has_a_detail_string():
    for check in checks().values():
        assert check.detail


# --------------------------------------------------------------- windows


def test_probe_windows_take_the_longest_ranges_inset():
    ranges = [
        TimeRange(start=0.0, end=0.3),
        TimeRange(start=10.0, end=12.0),
        TimeRange(start=20.0, end=21.0),
        TimeRange(start=30.0, end=33.0),
    ]
    windows = pick_probe_windows(ranges)
    assert len(windows) == 3
    assert windows[0].start == pytest.approx(10.0 + WINDOW_INSET_S)
    assert windows[0].end == pytest.approx(12.0 - WINDOW_INSET_S)


def test_probe_windows_are_returned_in_time_order():
    ranges = [TimeRange(start=30.0, end=33.0), TimeRange(start=10.0, end=12.0)]
    starts = [w.start for w in pick_probe_windows(ranges)]
    assert starts == sorted(starts)


def test_short_ranges_are_skipped():
    assert pick_probe_windows([TimeRange(start=1.0, end=1.1)]) == []


def test_a_range_that_insets_to_nothing_is_skipped():
    """Only reachable with a custom min_duration: the default (0.25 s) always
    exceeds twice the inset (0.08 s), so the guard is defensive."""
    ranges = [TimeRange(start=1.0, end=1.06)]
    assert pick_probe_windows(ranges, min_duration=0.05) == []
    assert pick_probe_windows(ranges, min_duration=0.05, inset=0.0) != []


def test_no_ranges_means_no_windows():
    assert pick_probe_windows([]) == []


def test_the_control_window_avoids_every_mute():
    ranges = [TimeRange(start=48.0, end=52.0)]
    window = pick_control_window(ranges, 100.0)
    assert window is not None
    guarded = TimeRange(start=47.5, end=52.5)
    assert not window.overlaps(guarded)


def test_the_control_window_prefers_the_midpoint():
    window = pick_control_window([], 100.0)
    assert window is not None
    assert window.start == pytest.approx(49.5)


def test_no_control_window_when_the_file_is_saturated():
    ranges = [TimeRange(start=0.0, end=100.0)]
    assert pick_control_window(ranges, 100.0) is None


def test_no_control_window_for_a_very_short_file():
    assert pick_control_window([], 0.5) is None


def test_the_control_window_stays_inside_the_file():
    window = pick_control_window([TimeRange(start=0.0, end=95.0)], 100.0)
    assert window is not None
    assert window.start >= 0.0 and window.end <= 100.0
