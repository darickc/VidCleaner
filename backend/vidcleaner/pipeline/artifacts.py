"""Typed artifacts written to ``/work/<job_id>/``.

ONE CLOCK -- read this before touching any time value
=====================================================

There are three clocks in play and confusing them mutes the wrong second of
every file, silently, in a way no unit test catches:

1. **Source container time.** What ffprobe reports and what subtitles use. It
   includes the audio stream's ``start_time``, which is non-zero for
   TS-derived rips and some MP4s.
2. **``audio.wav`` time**, always 0-based: WAV has no container timestamps.
3. **Filtergraph ``t``** during the render, which is input PTS as the filter
   sees it.

Every time field in ``probe.json``, ``subs.json``, ``transcript.json`` and
``detections.json`` is in **source container time**. ``stt`` is the only module
that adds ``probe.source_audio.start_time`` (recording exactly what it added in
``Transcript.audio_start_offset_s``), and ``render`` is the only module that
converts back. Nothing else does time arithmetic across clocks.

The runtime tripwire is verification's ``volumedetect`` check inside the mute
windows plus its control-window check outside them: together they catch a sign
error on every real job.

These are pydantic models rather than dataclasses on purpose. Artifacts are read
back *after a crash, possibly by different code*, so validation at the
deserialization boundary is the whole point -- and it is exactly where
dataclasses force a hand-written ``from_dict``. They also become FastAPI
``response_model``s in M4 with no adapter. Plain dataclasses are used for
in-process values that are never serialized (``StageContext``, ``FFmpegResult``).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from vidcleaner.pipeline.workspace import atomic_write_bytes

__all__ = [
    "BITMAP_SUBTITLE_CODECS",
    "TEXT_SUBTITLE_CODECS",
    "Artifact",
    "ArtifactError",
    "AudioStreamInfo",
    "Check",
    "CodecPlan",
    "Detection",
    "DriftProbe",
    "DriftResult",
    "DetectionResult",
    "JobSpec",
    "ProbeResult",
    "ProfileSnapshot",
    "RedactedSubtitle",
    "RenderResult",
    "SubtitleCue",
    "SubtitleHit",
    "SubtitleSource",
    "SubtitleStreamInfo",
    "SubtitlesResult",
    "TimeRange",
    "Transcript",
    "TranscriptSegment",
    "TranscriptWord",
    "VerifyResult",
    "VideoStreamInfo",
    "WordCount",
    "merge_ranges",
]

#: Redactable. ``text`` is what ffprobe reports for some MicroDVD/plain streams.
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"})
#: Passed through untouched. OCR (`pgsrip`) is a later option per §11.
BITMAP_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"})

SCHEMA_VERSION = 1


class ArtifactError(ValueError):
    """An artifact is missing, unreadable, or written by an incompatible schema."""


class Artifact(BaseModel):
    """Base for anything written to ``/work/<job_id>/``."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION

    @classmethod
    def read(cls, path: Path) -> Self:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ArtifactError(f"cannot read {path}: {exc}") from exc
        try:
            parsed = cls.model_validate_json(raw)
        except ValueError as exc:
            raise ArtifactError(f"cannot parse {path}: {exc}") from exc
        if parsed.schema_version != SCHEMA_VERSION:
            raise ArtifactError(
                f"{path}: schema_version {parsed.schema_version} != {SCHEMA_VERSION}"
            )
        return parsed

    def write(self, path: Path) -> Path:
        atomic_write_bytes(path, self.model_dump_json(indent=2).encode("utf-8"))
        return path


# --------------------------------------------------------------------- ranges


class TimeRange(BaseModel):
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def padded(
        self, pre_s: float, post_s: float, *, floor: float = 0.0, ceil: float | None = None
    ) -> TimeRange:
        start = max(floor, self.start - pre_s)
        end = self.end + post_s
        if ceil is not None:
            end = min(ceil, end)
        return TimeRange(start=start, end=max(start, end))

    def overlaps(self, other: TimeRange) -> bool:
        return self.start < other.end and other.start < self.end

    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0


def merge_ranges(ranges: Sequence[TimeRange], gap_s: float = 0.0) -> list[TimeRange]:
    """Sort and coalesce ranges that overlap or sit within ``gap_s``. Transitive."""
    usable = sorted((r for r in ranges if r.end > r.start), key=lambda r: (r.start, r.end))
    if not usable:
        return []
    out = [TimeRange(start=usable[0].start, end=usable[0].end)]
    for candidate in usable[1:]:
        current = out[-1]
        if candidate.start - current.end <= gap_s:
            if candidate.end > current.end:
                out[-1] = TimeRange(start=current.start, end=candidate.end)
        else:
            out.append(TimeRange(start=candidate.start, end=candidate.end))
    return out


def total_duration(ranges: Sequence[TimeRange]) -> float:
    return math.fsum(r.duration for r in ranges)


# -------------------------------------------------------------------- streams


class VideoStreamInfo(BaseModel):
    index: int
    typed_index: int
    codec_name: str
    width: int | None = None
    height: int | None = None
    pix_fmt: str | None = None
    avg_frame_rate: str | None = None
    duration: float | None = None
    is_attached_pic: bool = False


class AudioStreamInfo(BaseModel):
    index: int
    """Absolute ffprobe stream index."""
    typed_index: int
    """0-based within audio streams, i.e. the ``N`` in ``0:a:N``."""
    codec_name: str
    profile: str | None = None
    """e.g. ``DTS-HD MA``, ``Dolby Digital Plus + Dolby Atmos``."""
    channels: int = 2
    channel_layout: str | None = None
    sample_rate: int | None = None
    bit_rate: int | None = None
    """bits/s. Often absent for AAC in Matroska; see ``probe`` for the fallbacks."""
    bits_per_raw_sample: int | None = None
    language: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False
    dispositions: tuple[str, ...] = ()
    """Every non-zero disposition flag, so the render can preserve them."""
    start_time: float = 0.0
    """The offset ``stt`` applies. See the ONE CLOCK note at the top."""
    duration: float | None = None


class SubtitleStreamInfo(BaseModel):
    index: int
    typed_index: int
    codec_name: str
    language: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False
    dispositions: tuple[str, ...] = ()

    @property
    def is_text(self) -> bool:
        return self.codec_name in TEXT_SUBTITLE_CODECS

    @property
    def is_bitmap(self) -> bool:
        return self.codec_name in BITMAP_SUBTITLE_CODECS


class CodecPlan(BaseModel):
    """The chosen encoder for the clean track. Produced by ``codecs.py``."""

    encoder: Literal["aac", "ac3", "eac3", "flac"]
    bit_rate: int | None = None
    """bits/s; ``None`` for FLAC."""
    channels: int | None = None
    """``None`` keeps the source layout."""
    sample_rate: int | None = None
    extra_args: tuple[str, ...] = ()
    reason: str = ""
    """Stable token, asserted in tests and shown in the UI."""

    @property
    def is_lossless(self) -> bool:
        return self.encoder == "flac"


# ---------------------------------------------------------------------- probe


class ProbeResult(Artifact):
    path: str
    size: int
    mtime: float
    inode: int | None = None
    container_format: str = ""
    duration: float = 0.0
    fingerprint: str = ""
    """sha1(first 8 MB + last 8 MB + size), per PLAN.md §4."""
    tags: dict[str, str] = Field(default_factory=dict)
    video: list[VideoStreamInfo] = Field(default_factory=list)
    audio: list[AudioStreamInfo] = Field(default_factory=list)
    subtitles: list[SubtitleStreamInfo] = Field(default_factory=list)
    chapter_count: int = 0
    attachment_count: int = 0
    source_audio_typed_index: int = 0
    source_audio_reason: Literal["default", "preferred_language", "first"] = "first"
    clean_codec: CodecPlan
    already_clean: bool = False
    """The ``VIDCLEANER_PROFILE_HASH`` tag matches this job's profile hash."""

    @property
    def source_audio(self) -> AudioStreamInfo:
        for stream in self.audio:
            if stream.typed_index == self.source_audio_typed_index:
                return stream
        raise ArtifactError(
            f"probe.json names source audio a:{self.source_audio_typed_index} but it is absent"
        )

    @property
    def text_subtitles(self) -> list[SubtitleStreamInfo]:
        return [s for s in self.subtitles if s.is_text]


# ------------------------------------------------------------------ subtitles


class SubtitleCue(BaseModel):
    index: int
    start: float
    end: float
    text: str
    """Visible text, ASS override tags stripped."""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class SubtitleHit(BaseModel):
    """A word-list match inside one cue, with its proportional time span."""

    cue_index: int
    start: float
    end: float
    word_raw: str
    word_canonical: str
    category: str
    char_start: int
    char_end: int
    """Offsets into ``SubtitleCue.text``, for redaction and whitelist context."""


class SubtitleSource(BaseModel):
    kind: Literal["sidecar", "embedded", "none"] = "none"
    path: str | None = None
    stream_typed_index: int | None = None
    codec_name: str | None = None
    language: str | None = None
    reason: str = ""


class SubtitlesResult(Artifact):
    source: SubtitleSource = Field(default_factory=SubtitleSource)
    cues: list[SubtitleCue] = Field(default_factory=list)
    hits: list[SubtitleHit] = Field(default_factory=list)
    windows: list[TimeRange] = Field(default_factory=list)
    """Merged STT candidate windows (§6 step 3)."""
    offset_s: float = 0.0
    """Drift correction, added to every cue time. ``drift.py`` fills it in."""
    reliable: bool = True
    """False widens the windows; the timing is still offset-corrected. See ``drift``."""
    usable: bool = True
    """False means the cues do not describe this audio at all (wrong language or
    wrong episode). Distinct from ``reliable``: unusable subtitles are discarded
    and the job is promoted to a full-file pass, whereas unreliable ones are kept
    with wider windows. PLAN.md §6 conflates the two; see the Decision Log."""
    window_pad_s: float = 0.0
    """The padding actually applied to each cue. Recorded because ``reliable``
    widens it, and a resumed job must not have to re-derive which value was used."""
    redactable: list[int] = Field(default_factory=list)
    """Typed indexes of text subtitle streams whose language we can redact."""
    sidecars: list[str] = Field(default_factory=list)
    redactable_sidecars: list[str] = Field(default_factory=list)
    """Sidecar files to redact. A subset of ``sidecars``: see
    ``subtitles.redactable_sidecars`` for why an *untagged* sidecar is included
    here while an untagged embedded stream is not."""


# ---------------------------------------------------------------------- drift


class DriftProbe(BaseModel):
    """One sampled cue and what was actually heard there. Evidence, not control
    flow: when a job mutes the wrong second, this is the record that says why."""

    cue_index: int
    span: TimeRange | None = None
    cue_text: str = ""
    stt_text: str = ""
    pair_count: int = 0
    coverage: float = 0.0
    median_offset_s: float | None = None
    deltas: list[float] = Field(default_factory=list)
    """Capped; the full list is unbounded and nobody reads past the first few."""


class DriftResult(Artifact):
    """``drift.json`` -- the subtitle timing measurement (PLAN.md §6 step 3)."""

    checked: bool = False
    """False when the check was disabled, or too few cues were long enough."""
    model: str = ""
    action: Literal["ok", "unreliable", "discard", "skipped"] = "skipped"
    reason: str = ""
    offset_s: float = 0.0
    spread_s: float = 0.0
    """Spread of the *per-probe* medians: a growing offset means another cut."""
    coverage: float = 0.0
    """Fraction of cue words found in the audio. Replaces §6's text similarity,
    which a ±5 s probe pad makes unusable -- see the Decision Log."""
    elapsed_s: float = 0.0
    probes: list[DriftProbe] = Field(default_factory=list)


# ----------------------------------------------------------------- transcript


class TranscriptWord(BaseModel):
    word: str
    start: float
    end: float
    probability: float | None = None
    aligned: bool = False
    """True when whisperX supplied the timing rather than faster-whisper."""


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str = ""
    words: list[TranscriptWord] = Field(default_factory=list)


class Transcript(Artifact):
    mode: Literal["windowed", "full", "audit"] = "windowed"
    """The mode actually used, which is not always ``JobSpec.stt_mode``: a file
    with no usable subtitles is promoted to ``full``. ``detect`` reads this rather
    than re-deriving, so the matcher always matches the transcript it was handed."""
    mode_reason: str = ""
    """Why that mode. ``full_skipped_too_long`` is the one worth surfacing: it is
    the difference between "this file is clean" and "we declined to look"."""
    model: str = ""
    align_model: str | None = None
    language: str | None = None
    audio_start_offset_s: float = 0.0
    """Exactly what was added to every time. The audit trail for ONE CLOCK."""
    windows: list[TimeRange] = Field(default_factory=list)
    segments: list[TranscriptSegment] = Field(default_factory=list)
    dropped_out_of_window: int = 0
    """Guard counter: words that fell outside every requested window."""

    @property
    def words(self) -> Iterator[TranscriptWord]:
        for segment in self.segments:
            yield from segment.words

    @property
    def word_count(self) -> int:
        return sum(len(s.words) for s in self.segments)


# ----------------------------------------------------------------- detections


class Detection(BaseModel):
    """One profanity hit.

    Field-for-field identical to ``db.models.Detection`` minus ``job_id`` and
    ``media_item_id``, so ``persist.py`` is a mechanical copy rather than a
    mapping layer. ``suspicious_reason`` is JSON-only (no column).
    """

    word_raw: str
    word_canonical: str
    category: str
    start_s: float
    end_s: float
    mute_start_s: float
    mute_end_s: float
    source: Literal["subtitle", "stt", "both"]
    confidence: float | None = None
    muted: bool = True
    whitelisted: bool = False
    suspicious: bool = False
    suspicious_reason: str | None = None
    subtitle_cue_idx: int | None = None
    snippet_path: str | None = None

    @property
    def mute_range(self) -> TimeRange:
        return TimeRange(start=self.mute_start_s, end=self.mute_end_s)


class WordCount(BaseModel):
    """One row of §5's per-item rollup, precomputed so the CLI just prints it."""

    word_canonical: str
    category: str
    total: int
    muted: int
    suspicious: int
    sources: dict[str, int] = Field(default_factory=dict)


class DetectionResult(Artifact):
    profile_hash: str = ""
    detections: list[Detection] = Field(default_factory=list)
    mute_ranges: list[TimeRange] = Field(default_factory=list)
    """Padded and merged. The render's only input.

    Per-detection ``mute_start_s``/``mute_end_s`` stay **pre-merge**: §5's
    columns are per detection and §6 step 10's snippets need a per-word range.
    """
    counts: list[WordCount] = Field(default_factory=list)
    total_muted_s: float = 0.0
    stats: dict[str, int] = Field(default_factory=dict)

    @property
    def muted(self) -> list[Detection]:
        return [d for d in self.detections if d.muted and not d.whitelisted]

    @property
    def suspicious(self) -> list[Detection]:
        return [d for d in self.detections if d.suspicious]


# --------------------------------------------------------------------- render


class RedactedSubtitle(BaseModel):
    stream_typed_index: int | None = None
    sidecar_source: str | None = None
    output_path: str = ""
    replacements: int = 0
    tags_dropped: int = 0


class RenderResult(Artifact):
    out_path: str
    size: int = 0
    elapsed_s: float = 0.0
    encoder: str = ""
    bit_rate: int | None = None
    mute_range_count: int = 0
    filter_count: int = 0
    redacted: list[RedactedSubtitle] = Field(default_factory=list)
    tags: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- verify


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""
    severity: Literal["fatal", "warn"] = "fatal"
    expected: str | None = None
    actual: str | None = None


class VerifyResult(Artifact):
    ok: bool = False
    checks: list[Check] = Field(default_factory=list)
    measured_db: list[float] = Field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "fatal"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.severity == "warn"]


# ------------------------------------------------------------------- job spec


class ProfileSnapshot(BaseModel):
    """Everything about the profile that affects the mute set."""

    name: str = "Default"
    categories: list[str] = Field(default_factory=list)
    extra_canonicals: list[str] = Field(default_factory=list)
    pad_pre_ms: int = 80
    pad_post_ms: int = 120
    merge_gap_ms: int = 250
    mute_censored_tokens: bool = True
    profile_hash: str = ""


class JobTarget(BaseModel):
    """Which library item a worker job is for -- ids only, never credentials.

    ``refresh`` needs to know which series to rescan and which paths to tell
    Jellyfin about, but ``job.json`` deliberately carries no API keys (see
    ``JobSpec.settings``). Putting the *identity* on disk keeps the work dir
    self-describing, per CLAUDE.md, while the keys stay in the database and reach the
    stage through ``StageContext.integrations``.
    """

    media_item_id: int
    title_id: int
    kind: Literal["movie", "episode"] = "episode"
    arr_app: Literal["sonarr", "radarr"] | None = None
    arr_id: int | None = None
    """The series or movie id, not the file id."""
    arr_file_id: int | None = None
    season: int | None = None
    episode: int | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None


class JobSpec(Artifact):
    """``job.json`` -- artifact zero, so resume never needs the database."""

    job_id: str
    version: str
    source_path: str
    out_path: str | None = None
    dry_run: bool = False
    force: bool = False
    stt_mode: Literal["windowed", "full", "audit"] = "windowed"
    in_place: bool = False
    """Replace the library file (``swap``). Opt-in: inferring it from an absent
    ``out_path`` would make a bare ``vidcleaner clean file.mkv`` rewrite the library."""
    trigger: str = "manual"
    """§6.1's stability wait costs 10 s and only matters for ``webhook`` jobs, where
    the import may still be in flight; the trigger is what tells the runner which."""
    target: JobTarget | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    """``AppSettings`` minus ``SECRET_FIELDS``: /work ends up in bug reports."""
    profile: ProfileSnapshot = Field(default_factory=ProfileSnapshot)
    created_at: datetime | None = None

    @property
    def profile_hash(self) -> str:
        return self.profile.profile_hash
