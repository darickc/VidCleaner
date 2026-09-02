"""probe -> extract -> subtitles against real generated media."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidcleaner.matching.compiler import build_matcher
from vidcleaner.pipeline import extract
from vidcleaner.pipeline import probe as probe_stage
from vidcleaner.pipeline import subtitles as subs_stage
from vidcleaner.pipeline.artifacts import ProfileSnapshot
from vidcleaner.pipeline.ffmpeg import FFmpegRunner
from vidcleaner.pipeline.stages import build_context, build_spec, run_stage
from vidcleaner.settings_store import AppSettings


@pytest.fixture
def ctx_for(settings):
    def make(source: Path, *, settings_kw=None, **spec_kw):
        spec = build_spec(
            source,
            profile=ProfileSnapshot(profile_hash="v1:test"),
            # Off by default here too: these tests are about extraction and
            # matching, and drift has its own.
            settings=AppSettings(**{"drift_check": False, **(settings_kw or {})}),
            **spec_kw,
        )
        context = build_context(spec, deploy=settings)
        context.matcher = build_matcher()
        return context

    return make


# -------------------------------------------------------------------- probe


def test_probe_reads_the_generated_fixture(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    run_stage(ctx, "probe")
    result = probe_stage.load(ctx.ws)

    assert result.duration == pytest.approx(10.0, abs=0.2)
    assert len(result.audio) == 2
    assert len(result.subtitles) == 2
    assert result.chapter_count == 2
    assert result.fingerprint


def test_probe_picks_the_default_audio_stream(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    run_stage(ctx, "probe")
    result = probe_stage.load(ctx.ws)
    assert result.source_audio_reason == "default"
    assert result.source_audio.codec_name == "ac3"
    assert result.source_audio.channels == 6


def test_probe_chooses_ac3_640k_for_the_surround_source(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    run_stage(ctx, "probe")
    plan = probe_stage.load(ctx.ws).clean_codec
    assert (plan.encoder, plan.bit_rate, plan.reason) == ("ac3", 640_000, "ac3_passthrough")


def test_probe_preserves_the_comment_disposition(ctx_for, sample_mkv):
    """The C4 case: `-disposition 0` would wipe this on the output."""
    ctx = ctx_for(sample_mkv)
    run_stage(ctx, "probe")
    commentary = probe_stage.load(ctx.ws).audio[1]
    assert "comment" in commentary.dispositions
    assert not commentary.is_default


def test_probe_of_the_eight_channel_fixture_selects_flac(ctx_for, fixture_media):
    ctx = ctx_for(fixture_media.surround71_mkv)
    run_stage(ctx, "probe")
    result = probe_stage.load(ctx.ws)
    assert result.source_audio.channels == 8
    assert result.clean_codec.reason == "channels_gt_6"
    assert result.clean_codec.encoder == "flac"


def test_probe_reports_an_absent_language_as_none(ctx_for, fixture_media):
    ctx = ctx_for(fixture_media.nolang_mkv)
    run_stage(ctx, "probe")
    assert probe_stage.load(ctx.ws).source_audio.language is None


def test_probe_reads_the_stream_start_time(ctx_for, fixture_media):
    ctx = ctx_for(fixture_media.offset_mkv)
    run_stage(ctx, "probe")
    assert probe_stage.load(ctx.ws).source_audio.start_time == pytest.approx(0.5, abs=0.01)


def test_probe_of_the_mp4_sees_mov_text(ctx_for, sample_mp4):
    ctx = ctx_for(sample_mp4)
    run_stage(ctx, "probe")
    result = probe_stage.load(ctx.ws)
    assert result.container_format.startswith("mov")
    assert [s.codec_name for s in result.subtitles] == ["mov_text"]
    assert result.text_subtitles, "mov_text is a text codec"


def test_fingerprint_is_stable_and_size_sensitive(tmp_path, sample_mkv):
    first = probe_stage.fingerprint(sample_mkv)
    assert first == probe_stage.fingerprint(sample_mkv)

    with sample_mkv.open("ab") as handle:
        handle.write(b"\x00" * 1024)
    assert probe_stage.fingerprint(sample_mkv) != first


# ------------------------------------------------------------------ extract


def test_extract_produces_16k_mono_wav(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    run_stage(ctx, "probe")
    run_stage(ctx, "extract")

    assert ctx.ws.audio_wav.is_file()
    data = FFmpegRunner().probe(ctx.ws.audio_wav)
    stream = data["streams"][0]
    assert stream["codec_name"] == "pcm_s16le"
    assert int(stream["sample_rate"]) == extract.SAMPLE_RATE
    assert int(stream["channels"]) == 1


def test_extract_drops_the_stream_start_time(ctx_for, fixture_media):
    """The ONE CLOCK premise: wav_time = container_time - start_time.

    ffmpeg does not pad the extraction with the input offset, so a 10 s stream
    starting at +0.5 s yields a 10 s WAV -- not 10.5 s. `stt` adds the offset
    back exactly once.
    """
    ctx = ctx_for(fixture_media.offset_mkv)
    run_stage(ctx, "probe")
    run_stage(ctx, "extract")

    result = probe_stage.load(ctx.ws)
    assert result.source_audio.start_time == pytest.approx(0.5, abs=0.01)

    data = FFmpegRunner().probe(ctx.ws.audio_wav)
    wav_duration = float(data["format"]["duration"])
    assert wav_duration == pytest.approx(10.0, abs=0.15)


def test_extract_is_skipped_on_a_second_run(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    run_stage(ctx, "probe")
    run_stage(ctx, "extract")
    assert run_stage(ctx, "extract").skipped is True


# ---------------------------------------------------------------- subtitles


def test_subtitles_stage_extracts_and_matches(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    result = subs_stage.load(ctx.ws)

    assert result.source.kind == "embedded"
    assert result.source.language == "eng"
    assert len(result.cues) == 6
    assert [h.word_canonical for h in result.hits] == [
        "shit",
        "fuck",
        "god damn",
        "bullshit",
        "shit",
    ]


def test_subtitle_windows_cover_every_hit(ctx_for, sample_mkv):
    """Coverage is the correctness property; narrowing is a property of real media.

    This fixture is ten seconds with profanity in five of its six cues, so
    +/-1.5 s padding and a 2 s merge gap legitimately collapse to a single
    window spanning the file. Narrowing is asserted at the unit level
    (`test_distant_cues_stay_separate`) and holds strongly on real media, where
    the 56-minute M1 episode selects roughly a tenth of its runtime.
    """
    ctx = ctx_for(sample_mkv)
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    result = subs_stage.load(ctx.ws)

    assert result.windows
    for hit in result.hits:
        assert any(w.start <= hit.start and hit.end <= w.end for w in result.windows), hit


def test_only_the_english_subtitle_stream_is_redactable(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    result = subs_stage.load(ctx.ws)
    probe_result = probe_stage.load(ctx.ws)

    assert result.redactable == [0]
    spanish = next(s for s in probe_result.subtitles if s.language == "spa")
    assert spanish.typed_index not in result.redactable


def test_extracted_subtitle_lands_in_the_work_dir(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    result = subs_stage.load(ctx.ws)
    assert result.source.path is not None
    assert Path(result.source.path).parent == ctx.ws.subs_dir


def test_a_sidecar_beats_the_embedded_stream(ctx_for, sample_mkv):
    sidecar = sample_mkv.with_suffix("").with_name(f"{sample_mkv.stem}.eng.srt")
    sidecar.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nA different fucking line.\n", encoding="utf-8"
    )
    ctx = ctx_for(sample_mkv)
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    result = subs_stage.load(ctx.ws)

    assert result.source.kind == "sidecar"
    assert len(result.cues) == 1
    assert [h.word_canonical for h in result.hits] == ["fuck"]


def test_a_file_without_subtitles_yields_no_windows(ctx_for, fixture_media):
    ctx = ctx_for(fixture_media.nolang_mkv)
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)
    result = subs_stage.load(ctx.ws)

    assert result.source.kind == "none"
    assert result.source.reason == "no_subtitles"
    assert result.cues == [] and result.windows == []


def test_artifacts_are_written_and_reloadable(ctx_for, sample_mkv):
    ctx = ctx_for(sample_mkv)
    for stage in ("probe", "extract", "subtitles"):
        run_stage(ctx, stage)

    assert json.loads(ctx.ws.probe_json.read_text())["path"]
    assert json.loads(ctx.ws.subs_json.read_text())["cues"]
    assert ctx.ws.completed_stages() == ("probe", "extract", "subtitles")
