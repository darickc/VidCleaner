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
from collections.abc import Callable, Sequence
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

#: One ``whisperx.align`` call per batch of consecutive segments spanning at most
#: this much audio. A whole-file pass otherwise aligns two hours in a single call.
ALIGN_CHUNK_S = 300.0
#: And a count cap, for pathologically short segments.
ALIGN_CHUNK_SEGMENTS = 128
#: Recognition owns the first 80% of the progress bar and alignment the rest.
#: On a full pass alignment is comparable in cost to recognition, so reporting
#: 100% before it starts leaves the job apparently finished for many minutes.
RECOGNITION_PROGRESS_SHARE = 0.80

_MODEL_CACHE: dict[tuple[str, str, int], Any] = {}
_ALIGN_CACHE: dict[str, tuple[Any, Any]] = {}


def _windows_in_audio_time(request: TranscribeRequest) -> list[TimeRange]:
    """Shift container-time windows onto ``audio.wav``'s 0-based clock.

    ``subs.windows`` obey the ONE CLOCK rule -- source container time, like every
    other persisted value -- but faster-whisper reads ``audio.wav``, which has no
    container timestamps and starts at 0. Everything inside this module that
    compares against raw model output therefore works in audio time, and the
    offset is added back exactly once, in ``_to_segments``.
    """
    offset = request.time_offset_s
    if not offset:
        return list(request.windows)
    return [
        TimeRange(start=max(0.0, w.start - offset), end=max(0.0, w.end - offset))
        for w in request.windows
    ]


def _chunk_segments(
    segments: Sequence[dict],
    *,
    max_span_s: float = ALIGN_CHUNK_S,
    max_segments: int = ALIGN_CHUNK_SEGMENTS,
) -> list[list[dict]]:
    """Group *consecutive* segments into alignment batches.

    Pure, so the batching rule is unit-testable with no torch. Both caps matter:
    the span cap bounds how much audio one ``whisperx.align`` call holds, and the
    count cap catches the pathological case of thousands of sub-second segments
    inside one span.
    """
    batches: list[list[dict]] = []
    current: list[dict] = []
    start: float | None = None
    for segment in segments:
        seg_start = float(segment.get("start") or 0.0)
        seg_end = float(segment.get("end") or seg_start)
        if current and (
            len(current) >= max_segments or (start is not None and seg_end - start > max_span_s)
        ):
            batches.append(current)
            current, start = [], None
        if start is None:
            start = seg_start
        current.append(segment)
    if current:
        batches.append(current)
    return batches


def _merge_alignment(raw: Sequence[dict], aligned: Any) -> list[dict]:
    """Take whisperX word timings where it produced them, keeping segmentation.

    The previous implementation rebuilt **one** segment from whisperX's flat word
    list. That was tolerable for a few minutes of windows but wrong for any
    longer pass: a two-hour full transcript collapsed into a single
    ``TranscriptSegment`` holding ~20,000 words, with every segment's text
    concatenated into one string -- destroying exactly the structure
    ``transcript.json`` exists to carry.

    Each aligned word is now placed back into the raw segment whose span contains
    its midpoint, so segment boundaries and text survive. whisperX drops
    ``start``/``end`` for words it cannot align (numerals, out-of-vocabulary
    tokens); those segments keep their faster-whisper timings and report
    ``aligned=False``.
    """
    aligned_segments = (
        aligned.get("segments") if isinstance(aligned, dict) else getattr(aligned, "segments", None)
    )
    if not aligned_segments:
        return list(raw)

    words: list[dict] = []
    for segment in aligned_segments:
        for word in segment.get("words") or []:
            if word.get("start") is None or word.get("end") is None:
                continue
            words.append(
                {
                    "word": word.get("word", ""),
                    "start": float(word["start"]),
                    "end": float(word["end"]),
                    "probability": word.get("score"),
                    "aligned": True,
                }
            )
    if not words:
        return list(raw)

    out = [dict(segment) for segment in raw]
    bounds = [(float(s.get("start") or 0.0), float(s.get("end") or 0.0)) for s in out]
    buckets: list[list[dict]] = [[] for _ in out]
    for word in words:
        midpoint = (word["start"] + word["end"]) / 2.0
        index = _bucket_for(midpoint, bounds)
        buckets[index].append(word)

    for segment, bucket in zip(out, buckets, strict=True):
        if bucket:
            segment["words"] = bucket
    return out


def _bucket_for(midpoint: float, bounds: Sequence[tuple[float, float]]) -> int:
    """The segment containing ``midpoint``, else the nearest one.

    Alignment can nudge a word just outside its original segment; dropping it
    would lose a word, so it goes to the closest segment instead.
    """
    for index, (start, end) in enumerate(bounds):
        if start <= midpoint <= end:
            return index
    return min(
        range(len(bounds)),
        key=lambda i: min(abs(midpoint - bounds[i][0]), abs(midpoint - bounds[i][1])),
    )


class WhisperTranscriber:
    """faster-whisper for recognition, whisperX for word-level alignment."""

    name = "faster-whisper+whisperx"

    def __init__(self, *, device: str = "cpu", compute_type: str = "int8") -> None:
        self.device = device
        self.compute_type = compute_type

    # ------------------------------------------------------------- plumbing

    def _prepare_env(self, request: TranscribeRequest) -> int:
        """Thread and cache settings must be in place before torch loads.

        **The thread count from settings wins, and it is applied twice.** This used
        `setdefault`, which meant §10's shipped ``OMP_NUM_THREADS=6`` (in
        `docker-compose.yml` and the unraid template) silently beat the UI's "CPU
        threads": CTranslate2 got the setting, because `cpu_threads=` is passed
        directly, while torch and whisperX kept running at whatever the container
        said. The two halves of the same pipeline disagreed and the control looked
        broken.

        `setdefault` -> assignment fixes half of it. The other half is that libgomp
        reads ``OMP_NUM_THREADS`` **once**, at its own initialisation, so an
        in-process assignment is a no-op if anything has already pulled in torch --
        which is exactly why §10 puts the variable in the *entrypoint's* env contract.
        `torch.set_num_threads` is the API that works after the fact, and
        :meth:`_align` calls it. Both are needed: the env var for a cold process, the
        call for a warm one.
        """
        threads = resolve_threads(request.cpu_threads)
        os.environ["OMP_NUM_THREADS"] = str(threads)
        os.environ["MKL_NUM_THREADS"] = str(threads)
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

    def _align(
        self,
        segments: list[dict],
        language: str,
        audio_path: Path,
        on_progress: Callable[[float], None] | None = None,
        threads: int = 0,
    ) -> tuple[list[dict], str | None]:
        """whisperX forced alignment, batched, degrading gracefully per batch.

        Two properties matter more than they look. The audio is loaded **once**
        and the array reused, because a two-hour ``audio.wav`` is ~460 MB as
        float32 and reloading it per batch is the difference between working and
        thrashing. And a batch that raises keeps its native faster-whisper
        timings instead of dropping alignment for the whole file -- an extension
        of M1's "alignment is optional at runtime" to "alignment is optional
        *per batch*", which is strictly better on a long pass where one bad
        stretch of audio should not cost the other 119 minutes their accuracy.
        """
        try:
            import whisperx  # noqa: PLC0415
        except Exception as exc:  # ImportError, OSError from missing torch bits
            log.warning("stt.align_unavailable", error=str(exc))
            return segments, None

        if threads > 0:
            # The one thread control that works *after* torch has loaded. libgomp
            # reads OMP_NUM_THREADS once at its own initialisation, so setting the
            # env var in-process is a no-op by now -- which is why §10 puts it in the
            # entrypoint. Without this, the UI's "CPU threads" moved CTranslate2 and
            # left alignment running at whatever the container's env said.
            try:
                import torch  # noqa: PLC0415

                torch.set_num_threads(threads)
            except Exception as exc:  # noqa: BLE001 - never fail a job over a hint
                log.info("stt.thread_hint_failed", error=str(exc)[:200])

        try:
            if language not in _ALIGN_CACHE:
                _ALIGN_CACHE[language] = whisperx.load_align_model(
                    language_code=language, device=self.device
                )
            model, metadata = _ALIGN_CACHE[language]
        except Exception as exc:
            log.warning("stt.align_failed", error=str(exc), language=language, phase="load")
            return segments, None

        source: Any = str(audio_path)
        try:
            source = whisperx.load_audio(str(audio_path))
        except Exception as exc:  # older/newer whisperX, or a path-only API
            log.info("stt.align_audio_passthrough", error=str(exc))

        batches = _chunk_segments(segments)
        merged: list[dict] = []
        failed = 0
        for index, batch in enumerate(batches):
            try:
                aligned = whisperx.align(
                    batch,
                    model,
                    metadata,
                    source,
                    self.device,
                    return_char_alignments=False,
                )
            except Exception as exc:
                failed += 1
                log.warning(
                    "stt.align_batch_failed", error=str(exc), batch=index, segments=len(batch)
                )
                merged.extend(batch)
            else:
                merged.extend(_merge_alignment(batch, aligned))
            if on_progress is not None:
                on_progress((index + 1) / len(batches))

        if failed:
            log.warning("stt.align_partial", failed_batches=failed, batches=len(batches))
        if failed == len(batches):
            return segments, None
        name = getattr(metadata, "get", lambda *_: None)("model_name") or "wav2vec2"
        return merged, name

    # ---------------------------------------------------------------- public

    def transcribe(
        self,
        request: TranscribeRequest,
        on_progress: Callable[[float], None] | None = None,
    ) -> Transcript:
        threads = self._prepare_env(request)
        model = self._load_model(request, threads)

        audio_windows = _windows_in_audio_time(request)
        total = request.progress_total_s
        # A windowed pass accumulates segment durations against the window total.
        # A full pass must use *position* instead: with vad_filter=True the silent
        # stretches produce no segments at all, so an accumulator stalls partway
        # through and never reaches the end.
        by_position = not request.windows
        raw_segments: list[dict] = []

        generator, info = model.transcribe(
            str(request.audio_path),
            language=request.language,
            beam_size=request.beam_size,
            word_timestamps=True,
            vad_filter=request.vad_filter,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=request.condition_on_previous_text,
            initial_prompt=request.initial_prompt,
            clip_timestamps=self._clip_arg(audio_windows),
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
            if by_position:
                consumed = max(consumed, float(segment.end or 0.0))
            else:
                consumed += max(0.0, (segment.end or 0.0) - (segment.start or 0.0))
            if on_progress is not None and total:
                on_progress(min(1.0, consumed / total) * RECOGNITION_PROGRESS_SHARE)

        language = request.language or getattr(info, "language", None) or "en"
        align_model = None
        if request.align and raw_segments:

            def align_progress(fraction: float) -> None:
                if on_progress is not None:
                    share = 1.0 - RECOGNITION_PROGRESS_SHARE
                    on_progress(RECOGNITION_PROGRESS_SHARE + fraction * share)

            raw_segments, align_model = self._align(
                raw_segments, language, request.audio_path, align_progress, threads=threads
            )

        segments, dropped = self._to_segments(raw_segments, request)

        if on_progress is not None:
            on_progress(1.0)
        if dropped:
            log.warning(
                "stt.window_drift",
                dropped=dropped,
                hint="clip_timestamps may have changed semantics",
            )

        return self._build_transcript(
            request,
            segments=segments,
            language=language,
            align_model=align_model,
            dropped=dropped,
        )

    @staticmethod
    def _build_transcript(
        request: TranscribeRequest,
        *,
        segments: list[TranscriptSegment],
        language: str | None,
        align_model: str | None,
        dropped: int,
    ) -> Transcript:
        """Assemble the artifact. ``windows`` go back out in **container time**.

        They were only ever converted to audio time for use against the wav
        inside this module; ``transcript.json`` obeys the same clock as
        ``probe.json``, ``subs.json`` and ``detections.json``.
        """
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
    def _to_segments(
        raw: list[dict], request: TranscribeRequest
    ) -> tuple[list[TranscriptSegment], int]:
        offset = request.time_offset_s
        # Audio time: this runs on raw model output, before the offset is added.
        windows = _windows_in_audio_time(request)
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
