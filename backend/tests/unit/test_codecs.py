"""The codec policy table (PLAN.md §3). Pure -- no ffmpeg, no fixtures."""

from __future__ import annotations

import pytest

from vidcleaner.pipeline.artifacts import AudioStreamInfo
from vidcleaner.pipeline.codecs import (
    LOSSLESS_SOURCES,
    LOSSY_SOURCES,
    MAX_DOLBY_CHANNELS,
    SourceAudio,
    choose_clean_codec,
    from_stream,
)

# (codec, channels, source bitrate, profile, lossless) -> (encoder, bitrate, reason)
TABLE = [
    # --- AAC: floor of 160k per channel pair, ceiling of 256k per pair
    (("aac", 2, 128_000, None, False), ("aac", 160_000, "aac_passthrough")),
    (("aac", 2, 192_000, None, False), ("aac", 192_000, "aac_passthrough")),
    (("aac", 2, None, None, False), ("aac", 160_000, "aac_passthrough")),
    (("aac", 2, 900_000, None, False), ("aac", 256_000, "aac_passthrough")),
    (("aac", 1, 64_000, None, False), ("aac", 160_000, "aac_passthrough")),
    (("aac", 6, None, None, False), ("aac", 480_000, "aac_passthrough")),
    (("aac", 6, 640_000, None, False), ("aac", 640_000, "aac_passthrough")),
    # --- AC-3: 640k for surround, source-tracking for stereo
    (("ac3", 6, 448_000, None, False), ("ac3", 640_000, "ac3_passthrough")),
    (("ac3", 6, None, None, False), ("ac3", 640_000, "ac3_passthrough")),
    (("ac3", 2, 192_000, None, False), ("ac3", 192_000, "ac3_passthrough")),
    (("ac3", 2, None, None, False), ("ac3", 192_000, "ac3_passthrough")),
    (("ac3", 2, 640_000, None, False), ("ac3", 640_000, "ac3_passthrough")),
    # --- E-AC-3
    (("eac3", 6, 768_000, None, False), ("eac3", 768_000, "eac3_passthrough")),
    (("eac3", 6, None, None, False), ("eac3", 640_000, "eac3_passthrough")),
    (("eac3", 2, None, None, False), ("eac3", 224_000, "eac3_passthrough")),
    (
        ("eac3", 6, 384_000, "Dolby Digital Plus + Dolby Atmos", False),
        ("eac3", 640_000, "eac3_passthrough"),
    ),
    # --- the channel guard beats every Dolby rule
    (("eac3", 8, 1_536_000, None, False), ("flac", None, "channels_gt_6")),
    (("ac3", 8, 640_000, None, False), ("flac", None, "channels_gt_6")),
    # --- lossless / experimental-encoder sources
    (("dts", 6, 1_509_000, "DTS", False), ("flac", None, "lossless_source")),
    (("dts", 8, 4_000_000, "DTS-HD MA", False), ("flac", None, "channels_gt_6")),
    (("truehd", 6, None, "Dolby TrueHD + Dolby Atmos", False), ("flac", None, "truehd_atmos")),
    (("truehd", 6, None, None, False), ("flac", None, "lossless_source")),
    (("flac", 2, None, None, False), ("flac", None, "lossless_source")),
    (("pcm_s24le", 2, 2_304_000, None, False), ("flac", None, "lossless_source")),
    # --- other lossy codecs re-encode to AAC
    (("opus", 2, 128_000, None, False), ("aac", 160_000, "lossy_fallback_aac")),
    (("vorbis", 6, None, None, False), ("aac", 480_000, "lossy_fallback_aac")),
    (("mp3", 2, 320_000, None, False), ("aac", 256_000, "lossy_fallback_aac")),
    # --- forced lossless overrides everything
    (("ac3", 6, 448_000, None, True), ("flac", None, "forced_lossless")),
    (("aac", 2, 128_000, None, True), ("flac", None, "forced_lossless")),
    # --- unknown codecs
    (("wibble", 2, None, None, False), ("aac", 160_000, "unknown_codec_fallback")),
    (("", 2, None, None, False), ("aac", 160_000, "unknown_codec_fallback")),
]


def _id(case) -> str:
    (codec, channels, bitrate, profile, lossless), _ = case
    return f"{codec or 'empty'}-{channels}ch-{bitrate}-{'lossless' if lossless else 'lossy'}"


@pytest.mark.parametrize(("source", "expected"), TABLE, ids=[_id(c) for c in TABLE])
def test_codec_policy_table(source, expected):
    codec, channels, bitrate, profile, lossless = source
    plan = choose_clean_codec(SourceAudio(codec, channels, bitrate, profile), lossless=lossless)
    assert (plan.encoder, plan.bit_rate, plan.reason) == expected


# --------------------------------------------------------------- invariants


@pytest.mark.parametrize(("source", "expected"), TABLE, ids=[_id(c) for c in TABLE])
def test_lossy_encoders_always_carry_a_bitrate(source, expected):
    codec, channels, bitrate, profile, lossless = source
    plan = choose_clean_codec(SourceAudio(codec, channels, bitrate, profile), lossless=lossless)
    if plan.encoder == "flac":
        assert plan.bit_rate is None
    else:
        assert plan.bit_rate is not None and plan.bit_rate > 0


@pytest.mark.parametrize("channels", [7, 8, 12])
@pytest.mark.parametrize("codec", ["aac", "ac3", "eac3", "opus", "wibble"])
def test_more_than_six_channels_always_becomes_flac(codec, channels):
    """Neither ac3 nor eac3 can carry more than 5.1."""
    plan = choose_clean_codec(SourceAudio(codec, channels, 640_000))
    assert plan.encoder == "flac"
    assert channels > MAX_DOLBY_CHANNELS


@pytest.mark.parametrize("codec", sorted(LOSSLESS_SOURCES))
def test_every_lossless_source_maps_to_flac(codec):
    assert choose_clean_codec(SourceAudio(codec, 2)).encoder == "flac"


@pytest.mark.parametrize("codec", sorted(LOSSY_SOURCES))
def test_every_known_lossy_source_maps_to_aac(codec):
    assert choose_clean_codec(SourceAudio(codec, 2, 128_000)).encoder == "aac"


def test_reason_is_always_populated():
    for (codec, channels, bitrate, profile, lossless), _ in TABLE:
        plan = choose_clean_codec(SourceAudio(codec, channels, bitrate, profile), lossless=lossless)
        assert plan.reason


# ------------------------------------------------------------ bogus bitrates


@pytest.mark.parametrize("bogus", [0, 1, 31_999, 8_000_001, 999_999_999, -5])
def test_insane_source_bitrates_are_ignored(bogus):
    """A mis-reported bitrate must not drive an absurd re-encode."""
    plan = choose_clean_codec(SourceAudio("aac", 2, bogus))
    assert plan.bit_rate == 160_000


# -------------------------------------------------------------------- FLAC


def test_flac_uses_s16_for_16_bit_sources():
    plan = choose_clean_codec(SourceAudio("pcm_s16le", 2, bits_per_raw_sample=16))
    assert "s16" in plan.extra_args


def test_flac_uses_s32_for_deeper_sources():
    plan = choose_clean_codec(SourceAudio("pcm_s24le", 2, bits_per_raw_sample=24))
    assert "s32" in plan.extra_args


def test_flac_extra_args_are_deterministic():
    plan = choose_clean_codec(SourceAudio("flac", 2))
    assert plan.extra_args == ("-sample_fmt", "s16", "-compression_level", "5")


def test_flac_is_marked_lossless():
    assert choose_clean_codec(SourceAudio("dts", 6)).is_lossless


# ---------------------------------------------------------------- from_stream


def test_from_stream_lowercases_and_defaults():
    stream = AudioStreamInfo(
        index=1,
        typed_index=0,
        codec_name="EAC3",
        channels=6,
        bit_rate=768_000,
        profile="Dolby Digital Plus + Dolby Atmos",
    )
    source = from_stream(stream)
    assert source.codec_name == "eac3"
    assert choose_clean_codec(source).reason == "eac3_passthrough"


def test_from_stream_clamps_zero_channels():
    stream = AudioStreamInfo(index=1, typed_index=0, codec_name="aac", channels=0)
    assert from_stream(stream).channels == 1


def test_the_real_test_media_maps_to_eac3_at_source_bitrate():
    """PLURIBUS S01E01: eac3 5.1 Atmos at 768 kbps."""
    plan = choose_clean_codec(SourceAudio("eac3", 6, 768_000, "Dolby Digital Plus + Dolby Atmos"))
    assert (plan.encoder, plan.bit_rate) == ("eac3", 768_000)
