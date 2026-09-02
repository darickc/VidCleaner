"""ffprobe JSON parsing, against committed real and hand-written output.

The hand-written fixtures cover shapes that cannot be generated locally: a
DTS-HD MA 7.1 track (ffmpeg's `dca` encoder is experimental) and PGS/VobSub
subtitles (ffmpeg refuses text-to-bitmap transcoding). `eac3_atmos_many_subs`
is real, trimmed, output from the M1 test media.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidcleaner.pipeline.artifacts import ArtifactError
from vidcleaner.pipeline.probe import ProbeError, choose_source_audio, parse_probe

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "probe"


def payload(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def probe(name: str, **kw):
    defaults = dict(path=Path(f"/media/{name}.mkv"), size=1_000, mtime=1.0)
    return parse_probe(payload(name), **{**defaults, **kw})


# ------------------------------------------------- the real M1 test media


def test_real_media_stream_counts():
    result = probe("eac3_atmos_many_subs")
    assert len(result.video) == 1
    assert len(result.audio) == 1
    assert len(result.subtitles) == 5
    assert result.chapter_count == 0
    assert result.attachment_count == 0


def test_real_media_source_audio_is_the_default_eac3():
    result = probe("eac3_atmos_many_subs")
    audio = result.source_audio
    assert result.source_audio_reason == "default"
    assert audio.codec_name == "eac3"
    assert audio.channels == 6
    assert audio.bit_rate == 768_000
    assert audio.language == "eng"
    assert audio.profile == "Dolby Digital Plus + Dolby Atmos"


def test_real_media_atmos_maps_to_eac3_at_source_bitrate():
    plan = probe("eac3_atmos_many_subs").clean_codec
    assert (plan.encoder, plan.bit_rate, plan.reason) == ("eac3", 768_000, "eac3_passthrough")


def test_real_media_every_subtitle_is_text():
    result = probe("eac3_atmos_many_subs")
    assert len(result.text_subtitles) == len(result.subtitles)


def test_real_media_subtitles_without_a_language_tag_are_none_not_und():
    result = probe("eac3_atmos_many_subs")
    untagged = [s for s in result.subtitles if s.language is None]
    assert untagged, "the fixture includes streams with only a title"
    assert all(s.title for s in untagged)


def test_real_media_forced_disposition_is_captured():
    result = probe("eac3_atmos_many_subs")
    assert any(s.is_forced for s in result.subtitles)
    assert any(not s.is_forced for s in result.subtitles)


# ------------------------------------------------------------ DTS-HD / bitmap


def test_dts_hd_71_becomes_flac():
    plan = probe("dts_hd_bitmap_subs").clean_codec
    assert (plan.encoder, plan.reason) == ("flac", "channels_gt_6")
    assert "-sample_fmt" in plan.extra_args
    assert "s32" in plan.extra_args, "24-bit source should use s32"


def test_bitmap_subtitles_are_classified_as_bitmap():
    result = probe("dts_hd_bitmap_subs")
    bitmap = [s for s in result.subtitles if s.is_bitmap]
    text = result.text_subtitles
    assert {s.codec_name for s in bitmap} == {"hdmv_pgs_subtitle", "dvd_subtitle"}
    assert [s.codec_name for s in text] == ["subrip"]


def test_attachments_and_chapters_are_counted():
    result = probe("dts_hd_bitmap_subs")
    assert result.attachment_count == 1
    assert result.chapter_count == 2


def test_non_default_dispositions_are_preserved():
    """`comment` and `original` must survive so the render can restore them."""
    result = probe("dts_hd_bitmap_subs")
    commentary = result.audio[1]
    assert "comment" in commentary.dispositions
    assert not commentary.is_default
    assert "original" in result.audio[0].dispositions


def test_stereo_commentary_bitrate_is_read_from_the_stream():
    assert probe("dts_hd_bitmap_subs").audio[1].bit_rate == 192_000


# ------------------------------------------------------- bitrate fallbacks


def test_bitrate_falls_back_to_the_bps_tag():
    result = probe("aac_mkv_no_bitrate")
    assert result.audio[0].bit_rate == 192_000, "BPS-eng tag should be used"


def test_bitrate_falls_back_to_number_of_bytes_over_duration():
    result = probe("aac_mkv_no_bitrate")
    # 144_000_000 bytes * 8 / 1440 s
    assert result.audio[1].bit_rate == 800_000


def test_source_selection_falls_through_to_preferred_language():
    result = probe("aac_mkv_no_bitrate", preferred_language="eng")
    assert result.source_audio_reason == "preferred_language"
    assert result.source_audio.language == "eng"


def test_source_selection_honours_a_different_preference():
    result = probe("aac_mkv_no_bitrate", preferred_language="jpn")
    assert result.source_audio.language == "jpn"


def test_source_selection_falls_back_to_the_first_stream():
    result = probe("aac_mkv_no_bitrate", preferred_language="fre")
    assert result.source_audio_reason == "first"
    assert result.source_audio.typed_index == 0


# --------------------------------------------------------------- idempotency


def test_matching_profile_hash_marks_the_file_already_clean():
    result = probe("already_cleaned", profile_hash="v1:f1f199ce50dc81d9")
    assert result.already_clean is True
    assert result.tags["VIDCLEANER"] == "1"
    assert result.tags["VIDCLEANER_PROFILE_HASH"] == "v1:f1f199ce50dc81d9"


def test_a_different_profile_hash_is_not_already_clean():
    assert probe("already_cleaned", profile_hash="v1:something-else").already_clean is False


def test_no_profile_hash_never_reports_already_clean():
    assert probe("already_cleaned", profile_hash="").already_clean is False


def test_only_vidcleaner_tags_are_retained():
    result = probe("eac3_atmos_many_subs")
    assert all(k.startswith("VIDCLEANER") for k in result.tags)


# ------------------------------------------------------------------- offsets


def test_stream_start_time_is_captured():
    """The value stt applies and render undoes; see artifacts.py's ONE CLOCK note."""
    assert probe("offset_stream").source_audio.start_time == 1.4


def test_start_time_defaults_to_zero_when_absent():
    data = payload("offset_stream")
    for stream in data["streams"]:
        stream.pop("start_time", None)
    result = parse_probe(data, path=Path("/x.mkv"), size=1, mtime=1.0)
    assert result.source_audio.start_time == 0.0


# ------------------------------------------------------------------- degenerate


def test_a_file_with_no_audio_is_rejected():
    data = payload("offset_stream")
    data["streams"] = [s for s in data["streams"] if s["codec_type"] != "audio"]
    with pytest.raises(ProbeError, match="no audio streams"):
        parse_probe(data, path=Path("/x.mkv"), size=1, mtime=1.0)


def test_missing_format_duration_is_zero_not_a_crash():
    data = payload("offset_stream")
    data["format"].pop("duration")
    assert parse_probe(data, path=Path("/x.mkv"), size=1, mtime=1.0).duration == 0.0


def test_garbage_numeric_fields_do_not_crash():
    data = payload("offset_stream")
    data["streams"][1]["channels"] = "not a number"
    data["streams"][1]["bit_rate"] = "N/A"
    result = parse_probe(data, path=Path("/x.mkv"), size=1, mtime=1.0)
    assert result.source_audio.channels == 2


def test_attached_pic_video_is_flagged():
    data = payload("offset_stream")
    data["streams"].append(
        {
            "index": 9,
            "codec_name": "mjpeg",
            "codec_type": "video",
            "disposition": {"attached_pic": 1},
        }
    )
    result = parse_probe(data, path=Path("/x.mkv"), size=1, mtime=1.0)
    assert [v.is_attached_pic for v in result.video] == [False, True]


def test_typed_indexes_are_dense_and_ordered():
    result = probe("dts_hd_bitmap_subs")
    assert [a.typed_index for a in result.audio] == [0, 1]
    assert [s.typed_index for s in result.subtitles] == [0, 1, 2]


def test_probe_round_trips_through_json(tmp_path):
    result = probe("eac3_atmos_many_subs")
    from vidcleaner.pipeline.artifacts import ProbeResult

    assert ProbeResult.read(result.write(tmp_path / "probe.json")) == result


def test_source_audio_raises_if_the_index_is_bogus():
    result = probe("eac3_atmos_many_subs")
    broken = result.model_copy(update={"source_audio_typed_index": 99})
    with pytest.raises(ArtifactError):
        _ = broken.source_audio


# -------------------------------------------------------- choose_source_audio


def test_choose_source_audio_rejects_an_empty_list():
    with pytest.raises(ProbeError, match="no audio streams"):
        choose_source_audio([], "eng")
