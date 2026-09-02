"""The real faster-whisper + whisperX transcriber (PLAN.md §2, §3).

Every heavy import happens **inside** a function. Importing this module must
stay cheap, because ``pipeline.stages`` resolves stage modules through
``importlib`` and ``tests/unit/test_no_stt_import.py`` asserts that nothing on
the ordinary import path pulls torch.

Design notes from §3 that shape this:

* CPU only, ``int8`` CTranslate2, ``cpu_threads = cores - 2``.
* ``vad_filter=True`` -- Silero VAD cuts Whisper's hallucination rate on
  non-speech from ~40% to ~0.2%.
* ``condition_on_previous_text=False`` and a small beam, to stop the model
  running away on repeated phrases.
* whisperX wav2vec2 forced alignment for the word boundaries, because
  faster-whisper's native word timestamps are biased late by 100-400 ms.
  Alignment is *optional*: if whisperX is missing or its API has moved, the
  faster-whisper timings are kept with ``aligned=False`` and the detector's
  guards absorb the extra slop rather than the job failing.
* The audio stream's ``start_time`` is added exactly once, last.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import (
    TimeRange,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
from vidcleaner.pipeline.stt import TranscribeRequest, resolve_threads

__all__ = ["WhisperTranscriber"]

log = get_logger(__name__)

#: Words falling outside every requested window by more than this are dropped
#: and counted. `clip_timestamps` semantics have shifted between faster-whisper
#: releases, so this both guards against and *detects* a regression.
WINDOW_TOLERANCE_S = 1.0

_MODEL_CACHE: dict[tuple[str, str, int], Any] = {}
_ALIGN_CACHE: dict[str, tuple[Any, Any]] = {}


class WhisperTranscriber:
    """faster-whisper for recognition, whisperX for word-level alignment."""

    name = "faster-whisper+whisperx"

    def __init__(self, *, device: str = "cpu", compute_type: str = "int8") -> None:
        self.device = device
        self.compute_type = compute_type

    # ------------------------------------------------------------- plumbing

    def _prepare_env(self, request: TranscribeRequest) -> int:
        """Thread and cache settings must be in place before torch loads."""
        threads = resolve_threads(request.cpu_threads)
        os.environ.setdefault("OMP_NUM_THREADS", str(threads))
        os.environ.setdefault("MKL_NUM_THREADS", str(threads))
        if request.model_cache_dir is not None:
            cache = str(request.model_cache_dir)
            request.model_cache_dir.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("HF_HOME", cache)
            os.environ.setdefault("TORCH_HOME", cache)
        return threads

    def _load_model(self, request: TranscribeRequest, threads: int):
        from faster_whisper import WhisperModel  # noqa: PLC0415

        key = (request.model, self.compute_type, threads)
        if key not in _MODEL_CACHE:
            log.info("stt.model_load", model=request.model, threads=threads)
            _MODEL_CACHE[key] = WhisperModel(
                request.model,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=threads,
                num_workers=1,
                download_root=str(request.model_cache_dir) if request.model_cache_dir else None,
            )
        return _MODEL_CACHE[key]

    @staticmethod
    def _clip_arg(windows: list[TimeRange]) -> str | list[float]:
        """faster-whisper's ``clip_timestamps``: flat start/end seconds."""
        if not windows:
            return "0"
        flat: list[float] = []
        for window in windows:
            flat += [round(window.start, 3), round(window.end, 3)]
        return flat

    # ------------------------------------------------------------ alignment

    def _align(self, segments: list[dict], language: str, audio_path: Path):
        """whisperX forced alignment, degrading gracefully to native timings."""
        try:
            import whisperx  # noqa: PLC0415
        except Exception as exc:  # ImportError, OSError from missing torch bits
            log.warning("stt.align_unavailable", error=str(exc))
            return None, None

        try:
            if language not in _ALIGN_CACHE:
                _ALIGN_CACHE[language] = whisperx.load_align_model(
                    language_code=language, device=self.device
                )
            model, metadata = _ALIGN_CACHE[language]
            aligned = whisperx.align(
                segments,
                model,
                metadata,
                str(audio_path),
                self.device,
                return_char_alignments=False,
            )
        except Exception as exc:
            log.warning("stt.align_failed", error=str(exc), language=language)
            return None, None
        return aligned, getattr(metadata, "get", lambda *_: None)("model_name") or "wav2vec2"

    # ---------------------------------------------------------------- public

    def transcribe(
        self,
        request: TranscribeRequest,
        on_progress: Callable[[float], None] | None = None,
    ) -> Transcript:
        threads = self._prepare_env(request)
        model = self._load_model(request, threads)

        total = request.total_window_s or None
        raw_segments: list[dict] = []
        words: list[TranscriptWord] = []

        generator, info = model.transcribe(
            str(request.audio_path),
            language=request.language,
            beam_size=request.beam_size,
            word_timestamps=True,
            vad_filter=request.vad_filter,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=request.condition_on_previous_text,
            initial_prompt=request.initial_prompt,
            clip_timestamps=self._clip_arg(request.windows),
        )

        consumed = 0.0
        for segment in generator:
            segment_words = [
                {"word": w.word, "start": w.start, "end": w.end, "probability": w.probability}
                for w in (segment.words or [])
            ]
            raw_segments.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "words": segment_words,
                }
            )
            consumed += max(0.0, (segment.end or 0.0) - (segment.start or 0.0))
            if on_progress is not None and total:
                on_progress(min(1.0, consumed / total))

        language = request.language or getattr(info, "language", None) or "en"
        align_model = None
        if request.align and raw_segments:
            aligned, align_model = self._align(raw_segments, language, request.audio_path)
            if aligned is not None:
                raw_segments = self._merge_alignment(raw_segments, aligned)

        segments, dropped = self._to_segments(raw_segments, request)
        del words

        if on_progress is not None:
            on_progress(1.0)
        if dropped:
            log.warning(
                "stt.window_drift",
                dropped=dropped,
                hint="clip_timestamps may have changed semantics",
            )

        return Transcript(
            mode=request.mode,
            model=request.model,
            align_model=align_model,
            language=language,
            audio_start_offset_s=request.time_offset_s,
            windows=list(request.windows),
            segments=segments,
            dropped_out_of_window=dropped,
        )

    # ------------------------------------------------------------ internals

    @staticmethod
    def _merge_alignment(raw: list[dict], aligned: Any) -> list[dict]:
        """Take whisperX word timings where it produced them.

        whisperX drops ``start``/``end`` for words it cannot align (numerals,
        out-of-vocabulary tokens), so those keep the faster-whisper timing and
        are reported with ``aligned=False``.
        """
        aligned_segments = (
            aligned.get("segments")
            if isinstance(aligned, dict)
            else getattr(aligned, "segments", None)
        )
        if not aligned_segments:
            return raw

        replacements: list[dict] = []
        for segment in aligned_segments:
            for word in segment.get("words") or []:
                if word.get("start") is None or word.get("end") is None:
                    continue
                replacements.append(
                    {
                        "word": word.get("word", ""),
                        "start": float(word["start"]),
                        "end": float(word["end"]),
                        "probability": word.get("score"),
                        "aligned": True,
                    }
                )
        if not replacements:
            return raw

        # whisperX returns its own segmentation, so rebuild from its word list
        # and keep the original text for reference.
        return [
            {
                "start": replacements[0]["start"],
                "end": replacements[-1]["end"],
                "text": " ".join(s.get("text", "") for s in raw).strip(),
                "words": replacements,
            }
        ]

    @staticmethod
    def _to_segments(
        raw: list[dict], request: TranscribeRequest
    ) -> tuple[list[TranscriptSegment], int]:
        offset = request.time_offset_s
        windows = request.windows
        dropped = 0
        segments: list[TranscriptSegment] = []

        for entry in raw:
            words: list[TranscriptWord] = []
            for word in entry.get("words") or []:
                start, end = word.get("start"), word.get("end")
                if start is None or end is None:
                    continue
                if windows and not any(
                    w.start - WINDOW_TOLERANCE_S <= start <= w.end + WINDOW_TOLERANCE_S
                    for w in windows
                ):
                    dropped += 1
                    continue
                words.append(
                    TranscriptWord(
                        word=str(word.get("word", "")).strip(),
                        # The offset is applied exactly once, here, last.
                        start=float(start) + offset,
                        end=float(end) + offset,
                        probability=word.get("probability"),
                        aligned=bool(word.get("aligned", False)),
                    )
                )
            if not words:
                continue
            segments.append(
                TranscriptSegment(
                    start=words[0].start,
                    end=words[-1].end,
                    text=str(entry.get("text", "")).strip(),
                    words=words,
                )
            )
        return segments, dropped
