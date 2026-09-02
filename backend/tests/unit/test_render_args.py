"""The render argv and plan. Pure -- golden strings, no ffmpeg execution.

Stream-index arithmetic is the likeliest bug in this milestone, so the argv is
asserted rather than eyeballed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidcleaner.pipeline.artifacts import (
    AudioStreamInfo,
    CodecPlan,
    Detection,
    DetectionResult,
    ProbeResult,
    RedactedSubtitle,
    SubtitleStreamInfo,
    TimeRange,
)
from vidcleaner.pipeline.ffmpeg import FfmpegCaps
from vidcleaner.pipeline.probe import parse_probe
from vidcleaner.pipeline.render import (
    CLEAN_LABEL,
    build_render_command,
    plan_render,
    subtitle_out_codec,
)

CAPS = FfmpegCaps(
    ffmpeg="/usr/bin/ffmpeg",
    ffprobe="/usr/bin/ffprobe",
    version=(7, 1),
    filter_script_flag="-/filter_complex",
    encoders=frozenset({"aac", "ac3", "eac3", "flac"}),
)
GRAPH = Path("/work/j/graph.txt")
OUT = Path("/work/j/out.mkv")
PROBES = Path(__file__).resolve().parents[1] / "fixtures" / "probe"


def probe(
    *,
    audio: list[AudioStreamInfo] | None = None,
    subtitles: list[SubtitleStreamInfo] | None = None,
    source_index: int = 0,
    codec: CodecPlan | None = None,
    attachments: int = 0,
) -> ProbeResult:
    return ProbeResult(
        path="/media/in.mkv",
        size=1_000_000,
        mtime=1.0,
        duration=100.0,
        fingerprint="fp123",
        audio=audio
        or [
            AudioStreamInfo(
                index=1,
                typed_index=0,
                codec_name="ac3",
                channels=6,
                language="eng",
                is_default=True,
                dispositions=("default",),
            )
        ],
        subtitles=subtitles or [],
        attachment_count=attachments,
        source_audio_typed_index=source_index,
        clean_codec=codec or CodecPlan(encoder="ac3", bit_rate=640_000, reason="ac3_passthrough"),
    )


def detections(*ranges: TimeRange) -> DetectionResult:
    return DetectionResult(
        detections=[
            Detection(
                word_raw="x",
                word_canonical="x",
                category="strong",
                start_s=r.start,
                end_s=r.end,
                mute_start_s=r.start,
                mute_end_s=r.end,
                source="both",
            )
            for r in ranges
        ],
        mute_ranges=list(ranges),
    )


def build(p: ProbeResult, d: DetectionResult, **kw) -> list[str]:
    plan = plan_render(p, d, output=OUT, job_id="job-1", profile_hash="v1:hash", **kw)
    return build_render_command(plan, CAPS, GRAPH)


def argv_value(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


def argv_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


# ---------------------------------------------------------------- map order


def test_map_order_puts_video_first_then_clean_then_source_audio():
    args = build(probe(), detections(TimeRange(start=1.0, end=2.0)))
    assert argv_values(args, "-map")[:3] == ["0:V", f"[{CLEAN_LABEL}]", "0:a"]


def test_capital_v_excludes_attached_pictures():
    """MP4 cover art copied as a video stream into MKV is at best pointless."""
    assert "0:V" in argv_values(build(probe(), detections()), "-map")
    assert "0:v" not in argv_values(build(probe(), detections()), "-map")


def test_attachments_are_mapped_only_when_present():
    assert "0:t?" not in argv_values(build(probe(), detections()), "-map")
    assert "0:t?" in argv_values(build(probe(attachments=2), detections()), "-map")


def test_output_is_always_matroska():
    args = build(probe(), detections())
    assert argv_value(args, "-f") == "matroska"
    assert args[-1] == str(OUT)


def test_the_filter_script_flag_comes_from_caps():
    args = build(probe(), detections())
    assert "-/filter_complex" in args
    assert argv_value(args, "-/filter_complex") == str(GRAPH)
    assert "-filter_complex_script" not in args


def test_the_graph_is_never_passed_inline():
    args = build(probe(), detections(TimeRange(start=1.0, end=2.0)))
    assert not any("volume=0" in a for a in args)


# -------------------------------------------------------------- codecs


def test_blanket_copy_with_a_single_encoder_override():
    args = build(probe(), detections())
    assert argv_value(args, "-c:v") == "copy"
    assert argv_value(args, "-c:a") == "copy"
    assert argv_value(args, "-c:s") == "copy"
    assert argv_value(args, "-c:a:0") == "ac3"
    assert argv_value(args, "-b:a:0") == "640000"


def test_flac_passes_its_extra_args_and_no_bitrate():
    plan = CodecPlan(
        encoder="flac",
        extra_args=("-sample_fmt", "s32", "-compression_level", "5"),
        reason="lossless_source",
    )
    args = build(probe(codec=plan), detections())
    assert argv_value(args, "-c:a:0") == "flac"
    assert "-b:a:0" not in args
    assert "-sample_fmt" in args and "s32" in args


# -------------------------------------------------------- the a:(1+K) rule


def test_original_is_labelled_at_one_plus_the_source_ordinal():
    """`-map 0:a` preserves source order, so a hardcoded a:1 mislabels this."""
    audio = [
        AudioStreamInfo(index=1, typed_index=0, codec_name="aac", channels=2, language="jpn"),
        AudioStreamInfo(
            index=2,
            typed_index=1,
            codec_name="ac3",
            channels=6,
            language="eng",
            is_default=True,
            dispositions=("default",),
        ),
    ]
    args = build(probe(audio=audio, source_index=1), detections())
    assert "-metadata:s:a:2" in args
    assert argv_value(args, "-metadata:s:a:2") == "title=Original"
    assert argv_value(args, "-metadata:s:a:1") is None


def test_default_is_cleared_subtractively_on_every_default_source_track():
    """C4: a literal `0` zeroes the whole bitmask, destroying `comment` etc."""
    audio = [
        AudioStreamInfo(
            index=1,
            typed_index=0,
            codec_name="ac3",
            channels=6,
            is_default=True,
            dispositions=("default",),
        ),
        AudioStreamInfo(
            index=2,
            typed_index=1,
            codec_name="aac",
            channels=2,
            is_default=True,
            dispositions=("default", "comment"),
        ),
    ]
    args = build(probe(audio=audio), detections())
    assert argv_value(args, "-disposition:a:0") == "default"
    assert argv_value(args, "-disposition:a:1") == "-default"
    assert argv_value(args, "-disposition:a:2") == "-default"
    assert "0" not in argv_values(args, "-disposition:a:1")


def test_the_clean_track_is_marked_default():
    args = build(probe(), detections())
    assert argv_value(args, "-disposition:a:0") == "default"


# ---------------------------------------------------------------- metadata


def test_clean_track_title_and_language_are_explicit():
    """A `-map [label]` stream inherits nothing, not even under -map_metadata."""
    args = build(probe(), detections())
    values = argv_values(args, "-metadata:s:a:0")
    assert "title=Clean" in values
    assert "language=eng" in values


def test_an_absent_source_language_is_mirrored_as_absent():
    audio = [AudioStreamInfo(index=1, typed_index=0, codec_name="aac", channels=2, is_default=True)]
    args = build(probe(audio=audio), detections())
    values = argv_values(args, "-metadata:s:a:0")
    assert "title=Clean" in values
    assert not any(v.startswith("language=") for v in values)


def test_global_metadata_and_chapters_are_carried_over():
    args = build(probe(), detections())
    assert argv_value(args, "-map_metadata") == "0"
    assert argv_value(args, "-map_chapters") == "0"


def test_all_five_idempotency_tags_are_written():
    args = build(probe(), detections())
    tags = dict(v.split("=", 1) for v in argv_values(args, "-metadata"))
    assert tags["VIDCLEANER"] == "1"
    assert tags["VIDCLEANER_PROFILE_HASH"] == "v1:hash"
    assert tags["VIDCLEANER_JOB"] == "job-1"
    assert tags["VIDCLEANER_SRC_FP"] == "fp123"
    assert "VIDCLEANER_VERSION" in tags


# --------------------------------------------------------------- subtitles


@pytest.mark.parametrize(
    ("codec", "expected"),
    [
        ("subrip", "copy"),
        ("ass", "copy"),
        ("webvtt", "copy"),
        ("hdmv_pgs_subtitle", "copy"),
        ("dvd_subtitle", "copy"),
        ("mov_text", "srt"),
        ("text", "srt"),
    ],
)
def test_subtitle_output_codec_mapping(codec, expected):
    assert subtitle_out_codec(codec) == expected


def test_mov_text_is_transcoded_because_matroska_cannot_carry_it():
    subs = [SubtitleStreamInfo(index=2, typed_index=0, codec_name="mov_text", language="eng")]
    args = build(probe(subtitles=subs), detections())
    assert argv_value(args, "-c:s:0") == "srt"


def test_bitmap_subtitles_are_copied_and_never_given_an_extra_input():
    subs = [
        SubtitleStreamInfo(index=2, typed_index=0, codec_name="hdmv_pgs_subtitle", language="eng")
    ]
    args = build(probe(subtitles=subs), detections())
    assert "-c:s:0" not in args
    assert argv_values(args, "-i") == ["/media/in.mkv"]


def test_subtitle_map_order_follows_source_order_across_inputs():
    subs = [
        SubtitleStreamInfo(index=2, typed_index=0, codec_name="hdmv_pgs_subtitle"),
        SubtitleStreamInfo(index=3, typed_index=1, codec_name="subrip", language="eng"),
        SubtitleStreamInfo(index=4, typed_index=2, codec_name="subrip", language="spa"),
    ]
    redacted = {1: RedactedSubtitle(stream_typed_index=1, output_path="/work/red_1.srt")}
    args = build(probe(subtitles=subs), detections(), redacted=redacted)
    maps = argv_values(args, "-map")
    assert maps[3:6] == ["0:s:0", "1:s:0", "0:s:2"]


def test_a_redacted_stream_gets_its_metadata_restored():
    """A file input inherits nothing, so language/title/dispositions are re-set."""
    subs = [
        SubtitleStreamInfo(
            index=2,
            typed_index=0,
            codec_name="subrip",
            language="eng",
            title="English",
            is_default=True,
            dispositions=("default",),
        )
    ]
    redacted = {0: RedactedSubtitle(stream_typed_index=0, output_path="/work/red_0.srt")}
    args = build(probe(subtitles=subs), detections(), redacted=redacted)
    values = argv_values(args, "-metadata:s:s:0")
    assert "language=eng" in values
    assert "title=English" in values
    assert argv_value(args, "-disposition:s:0") == "default"


def test_an_unredacted_stream_keeps_its_inherited_metadata():
    subs = [
        SubtitleStreamInfo(
            index=2, typed_index=0, codec_name="subrip", language="eng", title="English"
        )
    ]
    args = build(probe(subtitles=subs), detections())
    assert "-metadata:s:s:0" not in args
    assert "-disposition:s:0" not in args


def test_redacted_input_extension_selects_the_codec():
    subs = [SubtitleStreamInfo(index=2, typed_index=0, codec_name="ass", language="eng")]
    redacted = {0: RedactedSubtitle(stream_typed_index=0, output_path="/work/red_0.ass")}
    args = build(probe(subtitles=subs), detections(), redacted=redacted)
    assert argv_value(args, "-c:s:0") == "ass"


# ------------------------------------------------------------------- plan


def test_plan_reports_the_original_ordinal():
    audio = [
        AudioStreamInfo(index=1, typed_index=0, codec_name="aac", channels=2),
        AudioStreamInfo(index=2, typed_index=1, codec_name="ac3", channels=6, is_default=True),
    ]
    plan = plan_render(
        probe(audio=audio, source_index=1),
        detections(),
        output=OUT,
        job_id="j",
        profile_hash="v1:h",
    )
    assert plan.original_output_ordinal == 2


def test_plan_builds_the_graph_from_the_source_stream():
    plan = plan_render(
        probe(),
        detections(TimeRange(start=1.0, end=2.0)),
        output=OUT,
        job_id="j",
        profile_hash="v1:h",
    )
    assert plan.graph.text.startswith("[0:a:0]")
    assert plan.graph.n_ranges == 1


def test_plan_with_no_detections_is_a_passthrough_graph():
    plan = plan_render(probe(), detections(), output=OUT, job_id="j", profile_hash="v1:h")
    assert "anull" in plan.graph.text


def test_extra_inputs_are_deduplicated_and_ordered():
    subs = [
        SubtitleStreamInfo(index=2, typed_index=0, codec_name="subrip", language="eng"),
        SubtitleStreamInfo(index=3, typed_index=1, codec_name="subrip", language="eng"),
    ]
    redacted = {
        0: RedactedSubtitle(stream_typed_index=0, output_path="/work/a.srt"),
        1: RedactedSubtitle(stream_typed_index=1, output_path="/work/b.srt"),
    }
    plan = plan_render(
        probe(subtitles=subs),
        detections(),
        output=OUT,
        job_id="j",
        profile_hash="v1:h",
        redacted=redacted,
    )
    assert plan.extra_inputs == ("/work/a.srt", "/work/b.srt")


# ------------------------------------------------ against the real M1 media


def test_the_real_media_plan_is_sane():
    payload = json.loads((PROBES / "eac3_atmos_many_subs.json").read_text())
    real = parse_probe(
        payload,
        path=Path("/media/PLURIBUS.mkv"),
        size=4_578_090_236,
        mtime=1.0,
        preferred_language="eng",
        fingerprint="fp",
    )
    args = build(real, detections(TimeRange(start=100.0, end=101.0)))

    assert argv_value(args, "-c:a:0") == "eac3"
    assert argv_value(args, "-b:a:0") == "768000"
    assert argv_value(args, "-metadata:s:a:1") == "title=Original"
    assert "language=eng" in argv_values(args, "-metadata:s:a:0")
    # every one of the fixture's subtitle streams is mapped, in order
    assert argv_values(args, "-map").count("0:s:0") == 1
    assert len([m for m in argv_values(args, "-map") if m.startswith("0:s:")]) == len(
        real.subtitles
    )
