"""Stage 4: speech-to-text over the candidate windows (PLAN.md §6 step 4).

This module imports nothing heavier than pydantic. The real faster-whisper and
whisperX work lives in ``whisper_backend``, imported lazily, so the API, the
worker, the matcher and every ffmpeg test run on a checkout without torch --
an invariant asserted by ``tests/unit/test_no_stt_import.py``.

``ScriptedTranscriber`` ships in production code rather than in ``tests/``
because it is genuinely useful: ``vidcleaner clean --transcript foo.json``
re-runs detect and render without repeating a 40-minute STT pass. It is also
what lets the whole integration tier exercise the pipeline with no torch
installed.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import (
    ProbeResult,
    SubtitlesResult,
    TimeRange,
    Transcript,
    TranscriptSegment,
)
from vidcleaner.pipeline.workspace import Workspace

NAME = "transcribe"

__all__ = [
    "NAME",
    "ScriptedTranscriber",
    "TranscribeRequest",
    "Transcriber",
    "build_initial_prompt",
    "get_transcriber",
    "load",
    "model_for_mode",
    "resolve_mode",
    "run",
]

log = get_logger(__name__)

#: Whisper's prompt window is 224 tokens and a long prompt measurably degrades
#: output, so the profanity hint is capped.
INITIAL_PROMPT_WORDS = 40


class TranscribeRequest(BaseModel):
    """Everything a transcriber needs. ``windows=[]`` means the whole file."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    audio_path: Path
    windows: list[TimeRange] = Field(default_factory=list)
    model: str = "large-v3-turbo"
    language: str | None = None
    """ISO 639-1, e.g. ``en``. ``None`` autodetects."""
    beam_size: int = 2
    vad_filter: bool = True
    condition_on_previous_text: bool = False
    initial_prompt: str | None = None
    cpu_threads: int = 0
    """0 = auto (cores - 2), per PLAN.md §4."""
    align: bool = True
    time_offset_s: float = 0.0
    """``probe.source_audio.start_time``. See the ONE CLOCK note in artifacts."""
    model_cache_dir: Path | None = None
    mode: Literal["windowed", "full", "audit"] = "windowed"
    duration_s: float = 0.0
    """The source duration. Only used as the progress denominator for a whole-file
    pass, where ``total_window_s`` is 0 and progress would otherwise never move."""

    @property
    def total_window_s(self) -> float:
        return sum(w.duration for w in self.windows)

    @property
    def progress_total_s(self) -> float | None:
        """Denominator for ``on_progress``. ``None`` when nothing is known."""
        return self.total_window_s or self.duration_s or None


@runtime_checkable
class Transcriber(Protocol):
    name: str

    def transcribe(
        self,
        request: TranscribeRequest,
        on_progress: Callable[[float], None] | None = None,
    ) -> Transcript: ...


class ScriptedTranscriber:
    """Replays a stored ``transcript.json``.

    Honours ``request.windows`` by filtering, so the windowing logic is still
    exercised rather than bypassed.
    """

    name = "scripted"

    def __init__(self, transcript: Transcript | Path | str) -> None:
        if isinstance(transcript, Transcript):
            self._transcript = transcript
        else:
            self._transcript = Transcript.read(Path(transcript))

    def transcribe(
        self,
        request: TranscribeRequest,
        on_progress: Callable[[float], None] | None = None,
    ) -> Transcript:
        segments: list[TranscriptSegment] = []
        for segment in self._transcript.segments:
            words = [
                word
                for word in segment.words
                if not request.windows
                or any(w.start <= word.start and word.end <= w.end for w in request.windows)
            ]
            if words:
                segments.append(
                    TranscriptSegment(
                        start=words[0].start,
                        end=words[-1].end,
                        text=segment.text,
                        words=words,
                    )
                )
        if on_progress is not None:
            on_progress(1.0)
        return self._transcript.model_copy(
            update={
                "segments": segments,
                "mode": request.mode,
                "windows": list(request.windows),
            }
        )


def model_for_mode(settings, mode: str) -> str:
    """PLAN.md §3: turbo for windowed passes, medium for full ones."""
    if mode == "windowed":
        return settings.stt_windowed_model
    if mode == "drift":
        return settings.stt_drift_model
    return settings.stt_full_model


def resolve_mode(
    spec_mode: str,
    subs: SubtitlesResult,
    *,
    duration_s: float,
    settings,
) -> tuple[str, str]:
    """The single place the effective STT mode is chosen. Returns ``(mode, reason)``.

    Both ``stt`` and ``detect`` need the answer, and before this existed they
    derived it independently -- ``stt`` from ``spec.stt_mode`` and ``detect`` from
    "are there any cues" -- so a file with unusable subtitles could be transcribed
    one way and matched the other. The answer is written to ``Transcript.mode``,
    which makes the artifact the source of truth and keeps a resumed job (or a
    ``--transcript`` replay) consistent with the run that produced it.

    An explicit ``full``/``audit`` always wins, and deliberately ignores the
    runtime cap: the cap exists to stop the pipeline *volunteering* for a
    multi-hour pass, not to overrule someone who asked for one.
    """
    if spec_mode != "windowed":
        return spec_mode, "explicit"
    if not subs.usable:
        reason = "subtitles_unusable"
    elif not subs.cues:
        reason = "no_subtitles"
    else:
        # Cues that parsed and simply contained no profanity are *evidence of a
        # clean file*, not missing information -- promoting on that would put
        # every clean episode through a full-file pass, which is the most
        # expensive thing this pipeline can do and would buy nothing. Only the
        # absence of usable subtitles justifies transcribing everything.
        return "windowed", "subtitles" if subs.windows else "no_candidate_windows"

    cap_s = float(getattr(settings, "stt_full_max_hours", 0.0) or 0.0) * 3600.0
    if cap_s and duration_s > cap_s:
        return "windowed", "full_skipped_too_long"
    return "full", reason


def resolve_threads(cpu_threads: int = 0) -> int:
    """§4: ``cores - 2`` for CTranslate2, leaving room for the API and ffmpeg."""
    if cpu_threads > 0:
        return cpu_threads
    return max(1, (os.cpu_count() or 4) - 2)


def build_initial_prompt(matcher, limit: int = INITIAL_PROMPT_WORDS) -> str | None:
    """A cheap hint that Whisper should not sanitise what it hears (§3).

    Capped: Whisper's prompt window is 224 tokens and a long prompt measurably
    degrades transcription quality.
    """
    words = sorted({e.canonical for e in matcher.entries if not e.is_phrase})[:limit]
    if not words:
        return None
    return "The following transcript may contain explicit language: " + ", ".join(words) + "."


def get_transcriber(settings, *, mode: str = "windowed") -> Transcriber:
    """Build the real transcriber. Imports torch, so it is called late."""
    from vidcleaner.pipeline.whisper_backend import WhisperTranscriber  # noqa: PLC0415

    return WhisperTranscriber()


def build_request(
    ctx, probe: ProbeResult, subs: SubtitlesResult, *, mode: str
) -> TranscribeRequest:
    from vidcleaner.pipeline import lang  # noqa: PLC0415

    prompt = None
    if ctx.settings.initial_prompt_hint and ctx.matcher is not None:
        prompt = build_initial_prompt(ctx.matcher)

    windows: Sequence[TimeRange] = subs.windows if mode == "windowed" else []
    return TranscribeRequest(
        audio_path=ctx.ws.audio_wav,
        windows=list(windows),
        model=model_for_mode(ctx.settings, mode),
        language=lang.to_iso639_1(subs.source.language or ctx.settings.preferred_language),
        beam_size=ctx.settings.beam_size,
        vad_filter=ctx.settings.vad_filter,
        initial_prompt=prompt,
        cpu_threads=ctx.settings.cpu_threads,
        # The single place the audio.wav -> container-time offset is applied.
        time_offset_s=probe.source_audio.start_time,
        model_cache_dir=ctx.deploy.config_dir / "models",
        mode=mode,  # type: ignore[arg-type]
        duration_s=probe.duration,
    )


def run(ctx) -> None:
    probe = ProbeResult.read(ctx.ws.probe_json)
    subs = SubtitlesResult.read(ctx.ws.subs_json)

    if ctx.matcher is None:
        from vidcleaner.matching.compiler import build_matcher  # noqa: PLC0415

        ctx.matcher = build_matcher()

    mode, reason = resolve_mode(
        ctx.spec.stt_mode, subs, duration_s=probe.duration, settings=ctx.settings
    )
    if mode == "windowed" and not subs.windows:
        # Either no cue contained a word-list hit (nothing to transcribe), or a
        # promotion to full was refused by the runtime cap. Either way an empty
        # transcript is the correct artifact -- but it records *which*, so the
        # CLI and M3's job row can tell "clean file" from "we gave up".
        Transcript(
            mode="windowed",
            model=model_for_mode(ctx.settings, mode),
            audio_start_offset_s=probe.source_audio.start_time,
            mode_reason=reason,
        ).write(ctx.ws.transcript_json)
        if reason == "full_skipped_too_long":
            ctx.log.warning(
                "transcribe.full_skipped",
                reason=reason,
                duration_s=round(probe.duration, 1),
                cap_hours=ctx.settings.stt_full_max_hours,
            )
        else:
            ctx.log.info("transcribe.skipped", reason=reason)
        return

    request = build_request(ctx, probe, subs, mode=mode)
    transcriber = ctx.transcriber or get_transcriber(ctx.settings, mode=mode)

    ctx.log.info(
        "transcribe.start",
        transcriber=transcriber.name,
        model=request.model,
        mode=mode,
        mode_reason=reason,
        windows=len(request.windows),
        window_seconds=round(request.total_window_s, 1),
        language=request.language,
        threads=resolve_threads(request.cpu_threads),
    )
    transcript = transcriber.transcribe(request, on_progress=lambda f: ctx.progress(NAME, f))
    # The transcriber reports what it did; the *why* is the stage's to record.
    transcript = transcript.model_copy(update={"mode_reason": reason})
    transcript.write(ctx.ws.transcript_json)
    ctx.log.info(
        "transcribe.done",
        words=transcript.word_count,
        segments=len(transcript.segments),
        aligned=sum(1 for w in transcript.words if w.aligned),
        dropped_out_of_window=transcript.dropped_out_of_window,
        align_model=transcript.align_model,
    )


def load(ws: Workspace) -> Transcript | None:
    return Transcript.read(ws.transcript_json) if ws.transcript_json.is_file() else None
