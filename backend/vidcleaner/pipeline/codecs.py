"""Choosing the encoder for the clean track (PLAN.md §3's codec policy).

Pure and exhaustively table-tested: no I/O, no settings object, no ffmpeg. The
caller passes ``lossless=settings.clean_track_lossless``.

The policy exists because filtering forces a re-encode of the clean track only,
and several source formats have no usable ffmpeg encoder: ``dca`` and ``truehd``
are experimental, and neither ``ac3`` nor ``eac3`` can carry more than 5.1
channels. Anything in those buckets becomes FLAC, which is lossless and always
available. The untouched original track is retained either way, so nothing is
actually lost -- an Atmos source keeps its Atmos on track 2.
"""

from __future__ import annotations

from dataclasses import dataclass

from vidcleaner.pipeline.artifacts import AudioStreamInfo, CodecPlan

__all__ = ["SourceAudio", "choose_clean_codec", "from_stream"]

#: Sources with no practical lossy re-encode path: either lossless already, or
#: ffmpeg's encoder is experimental/absent.
LOSSLESS_SOURCES = frozenset(
    {
        "dts",
        "dts_hd",
        "truehd",
        "mlp",
        "flac",
        "alac",
        "wavpack",
        "tta",
        "ape",
        "dsd_lsbf",
        "dsd_msbf",
        "dsd_lsbf_planar",
        "dsd_msbf_planar",
        "pcm_s16le",
        "pcm_s16be",
        "pcm_s24le",
        "pcm_s24be",
        "pcm_s32le",
        "pcm_s32be",
        "pcm_f32le",
        "pcm_f64le",
        "pcm_dvd",
        "pcm_bluray",
    }
)

#: Lossy sources with no matching ffmpeg encoder worth keeping; re-encode to AAC.
LOSSY_SOURCES = frozenset(
    {"opus", "vorbis", "mp3", "mp2", "mp1", "wmav1", "wmav2", "wmapro", "cook", "sipr", "amrnb"}
)

#: E-AC-3 genuinely stops at 5.1, as does AC-3.
MAX_DOLBY_CHANNELS = 6

_AAC_PER_PAIR_FLOOR = 160_000
_AAC_PER_PAIR_CEIL = 256_000
_AC3_SURROUND = 640_000
_AC3_STEREO_FLOOR = 192_000
_EAC3_CEIL = 1_024_000
_EAC3_DEFAULTS = {1: 96_000, 2: 224_000, 3: 384_000, 4: 384_000, 5: 640_000, 6: 640_000}

_MIN_SANE_BITRATE = 32_000
_MAX_SANE_BITRATE = 8_000_000


@dataclass(frozen=True, slots=True)
class SourceAudio:
    codec_name: str
    channels: int = 2
    bit_rate: int | None = None
    profile: str | None = None
    bits_per_raw_sample: int | None = None


def from_stream(stream: AudioStreamInfo) -> SourceAudio:
    return SourceAudio(
        codec_name=(stream.codec_name or "").lower(),
        channels=max(1, stream.channels),
        bit_rate=stream.bit_rate,
        profile=stream.profile,
        bits_per_raw_sample=stream.bits_per_raw_sample,
    )


def _pairs(channels: int) -> int:
    return (channels + 1) // 2


def _sane(bit_rate: int | None) -> int | None:
    """Discard obviously bogus ffprobe/tag values rather than trusting them."""
    if bit_rate is None or not (_MIN_SANE_BITRATE <= bit_rate <= _MAX_SANE_BITRATE):
        return None
    return bit_rate


def _aac_bitrate(source: SourceAudio) -> int:
    floor = _AAC_PER_PAIR_FLOOR * _pairs(source.channels)
    ceil = _AAC_PER_PAIR_CEIL * _pairs(source.channels)
    return max(floor, min(ceil, _sane(source.bit_rate) or 0))


def _ac3_bitrate(source: SourceAudio) -> int:
    if source.channels >= 3:
        return _AC3_SURROUND
    # Deviation from §3's flat 640k, logged in §14: a two-hour *stereo* AC-3
    # track at 640k is 576 MB against 173 MB at 192k for no audible gain, and
    # this file is added to every episode in the library. 5.1 keeps 640k.
    return min(_AC3_SURROUND, max(_AC3_STEREO_FLOOR, _sane(source.bit_rate) or 0))


def _eac3_bitrate(source: SourceAudio) -> int:
    default = _EAC3_DEFAULTS.get(source.channels, _AC3_SURROUND)
    return max(default, min(_EAC3_CEIL, _sane(source.bit_rate) or 0))


def _flac(source: SourceAudio, reason: str) -> CodecPlan:
    depth = source.bits_per_raw_sample or 16
    # Explicit rather than auto-negotiated so the argv stays deterministic and
    # golden-testable. The FLAC encoder accepts only s16 and s32.
    sample_fmt = "s32" if depth > 16 else "s16"
    return CodecPlan(
        encoder="flac",
        bit_rate=None,
        extra_args=("-sample_fmt", sample_fmt, "-compression_level", "5"),
        reason=reason,
    )


def choose_clean_codec(source: SourceAudio, *, lossless: bool = False) -> CodecPlan:
    """Pick the clean track's encoder. First matching rule wins.

    ``reason`` is a stable token: it is asserted in tests and shown in the UI.
    """
    codec = (source.codec_name or "").lower()
    profile = (source.profile or "").lower()

    if lossless:
        return _flac(source, "forced_lossless")

    # Before the ac3/eac3 rules on purpose: neither encoder can do 7.1.
    if source.channels > MAX_DOLBY_CHANNELS:
        return _flac(source, "channels_gt_6")

    if codec == "truehd" and "atmos" in profile:
        return _flac(source, "truehd_atmos")

    if codec in LOSSLESS_SOURCES:
        return _flac(source, "lossless_source")

    if codec == "eac3":
        # E-AC-3 + Atmos (JOC) lands here deliberately: the clean track becomes
        # plain E-AC-3 and loses the Atmos objects, while the untouched original
        # keeps them one track-switch away. Accepted 2026-09-01.
        return CodecPlan(encoder="eac3", bit_rate=_eac3_bitrate(source), reason="eac3_passthrough")

    if codec == "ac3":
        return CodecPlan(encoder="ac3", bit_rate=_ac3_bitrate(source), reason="ac3_passthrough")

    if codec == "aac":
        return CodecPlan(encoder="aac", bit_rate=_aac_bitrate(source), reason="aac_passthrough")

    if codec in LOSSY_SOURCES:
        return CodecPlan(encoder="aac", bit_rate=_aac_bitrate(source), reason="lossy_fallback_aac")

    # Unknown codec, <= 5.1 channels. AAC is universally playable; the original
    # is retained regardless. (There is no ">6 channels" branch here: the
    # channel guard above already claimed those.)
    return CodecPlan(encoder="aac", bit_rate=_aac_bitrate(source), reason="unknown_codec_fallback")
