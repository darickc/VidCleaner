"""The real faster-whisper + whisperX backend.

Skipped unless the `stt` extra is installed. Uses `tiny` and a few seconds of
audio, so it stays a plumbing check rather than a quality benchmark -- model
quality is judged against real media by hand, and recorded in PLAN.md §14.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.pipeline.artifacts import TimeRange, Transcript
from vidcleaner.pipeline.stt import TranscribeRequest, resolve_threads

pytest.importorskip("faster_whisper", reason="the `stt` extra is not installed")


@pytest.fixture(scope="module")
def transcriber():
    from vidcleaner.pipeline.whisper_backend import WhisperTranscriber

    return WhisperTranscriber()


@pytest.fixture
def audio_wav(sample_mkv, tmp_path, runner) -> Path:
    """16 kHz mono WAV, exactly what the extract stage produces."""
    target = tmp_path / "audio.wav"
    runner.run(
        [
            "-i",
            str(sample_mkv),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(target),
        ],
        label="extract",
    )
    return target


def request(audio: Path, **kw) -> TranscribeRequest:
    defaults = dict(audio_path=audio, model="tiny", language="en", align=False)
    return TranscribeRequest(**{**defaults, **kw})


def test_the_backend_returns_a_transcript(transcriber, audio_wav):
    result = transcriber.transcribe(request(audio_wav))
    assert isinstance(result, Transcript)
    assert result.model == "tiny"
    assert result.language == "en"


def test_vad_suppresses_hallucination_on_non_speech(transcriber, audio_wav):
    """PLAN.md §3: Whisper hallucinates on ~40% of non-speech clips; Silero VAD
    cuts that to ~0.2%. The fixture audio is a pure 1 kHz tone, so a
    VAD-filtered pass should find no words at all."""
    result = transcriber.transcribe(request(audio_wav, vad_filter=True))
    assert result.word_count == 0, f"hallucinated: {[w.word for w in result.words]}"


def test_windows_are_respected_and_drift_is_counted(transcriber, audio_wav):
    """The `clip_timestamps` guard: a semantics change shows up as a counter."""
    result = transcriber.transcribe(request(audio_wav, windows=[TimeRange(start=2.0, end=4.0)]))
    assert result.dropped_out_of_window == 0
    assert result.windows == [TimeRange(start=2.0, end=4.0)]
    for word in result.words:
        assert 1.0 <= word.start <= 5.0


def test_the_time_offset_is_recorded_and_applied(transcriber, audio_wav):
    result = transcriber.transcribe(request(audio_wav, time_offset_s=7.5, vad_filter=False))
    assert result.audio_start_offset_s == 7.5
    for word in result.words:
        assert word.start >= 7.0, "the offset must be added to every word"


def test_the_progress_callback_reaches_one(transcriber, audio_wav):
    seen: list[float] = []
    transcriber.transcribe(
        request(audio_wav, windows=[TimeRange(start=0.0, end=4.0)]),
        on_progress=seen.append,
    )
    assert seen and seen[-1] == 1.0


def test_alignment_degrades_gracefully_when_whisperx_is_missing(
    transcriber, audio_wav, monkeypatch
):
    """A broken whisperX must cost timing accuracy, never the whole job."""
    import builtins

    real_import = builtins.__import__

    def fail_whisperx(name, *args, **kwargs):
        if name == "whisperx":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_whisperx)
    result = transcriber.transcribe(request(audio_wav, align=True, vad_filter=False))
    assert result.align_model is None
    assert all(not w.aligned for w in result.words)


def test_threads_are_capped_below_the_core_count():
    import os

    assert resolve_threads(0) <= max(1, os.cpu_count() or 4)
