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

    @property
    def total_window_s(self) -> float:
        return sum(w.duration for w in self.windows)


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
    )


def run(ctx) -> None:
    probe = ProbeResult.read(ctx.ws.probe_json)
    subs = SubtitlesResult.read(ctx.ws.subs_json)

    if ctx.matcher is None:
        from vidcleaner.matching.compiler import build_matcher  # noqa: PLC0415

        ctx.matcher = build_matcher()

    mode = ctx.spec.stt_mode
    if mode == "windowed" and not subs.windows:
        # Nothing to transcribe: no cue contained a word-list hit. An empty
        # transcript is the correct artifact, and M2's full mode is the
        # fallback for files with no usable subtitles at all.
        Transcript(
            mode="windowed",
            model=model_for_mode(ctx.settings, mode),
            audio_start_offset_s=probe.source_audio.start_time,
        ).write(ctx.ws.transcript_json)
        ctx.log.info("transcribe.skipped", reason="no candidate windows")
        return

    request = build_request(ctx, probe, subs, mode=mode)
    transcriber = ctx.transcriber or get_transcriber(ctx.settings, mode=mode)

    ctx.log.info(
        "transcribe.start",
        transcriber=transcriber.name,
        model=request.model,
        mode=mode,
        windows=len(request.windows),
        window_seconds=round(request.total_window_s, 1),
        language=request.language,
        threads=resolve_threads(request.cpu_threads),
    )
    transcript = transcriber.transcribe(request, on_progress=lambda f: ctx.progress(NAME, f))
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
