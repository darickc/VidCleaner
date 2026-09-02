"""Windows cross the ONE CLOCK boundary exactly once (see artifacts.py's header).

``subs.windows`` are **source container time**, like every other persisted time.
``audio.wav`` is 0-based. So the windows have to be shifted by the audio stream's
``start_time`` before they can be used against the wav -- as ``clip_timestamps``
and as the out-of-window filter -- and shifted back before they are persisted.

This is invisible on every fixture and every file whose ``start_time`` is 0,
which is nearly all of them; it bites on the TS-derived rips ONE CLOCK exists
for, and it is exactly the kind of sign error that mutes the wrong second of a
file while every structural check still passes.
"""

from __future__ import annotations

from vidcleaner.pipeline.artifacts import TimeRange
from vidcleaner.pipeline.stt import TranscribeRequest
from vidcleaner.pipeline.whisper_backend import WhisperTranscriber, _windows_in_audio_time

OFFSET = 0.5


def request(**kwargs) -> TranscribeRequest:
    base = {
        "audio_path": "a.wav",
        "windows": [TimeRange(start=10.5, end=15.5)],
        "time_offset_s": OFFSET,
    }
    return TranscribeRequest(**{**base, **kwargs})


def test_windows_are_shifted_into_audio_time():
    """Container 10.5-15.5 with a 0.5 s stream offset is wav 10.0-15.0."""
    shifted = _windows_in_audio_time(request())
    assert [(w.start, w.end) for w in shifted] == [(10.0, 15.0)]


def test_no_offset_is_a_no_op():
    shifted = _windows_in_audio_time(request(time_offset_s=0.0))
    assert [(w.start, w.end) for w in shifted] == [(10.5, 15.5)]


def test_clip_timestamps_are_in_audio_time():
    """faster-whisper reads audio.wav, which knows nothing about the container."""
    assert WhisperTranscriber._clip_arg(_windows_in_audio_time(request())) == [10.0, 15.0]


def test_a_window_cannot_be_shifted_before_the_start_of_the_audio():
    shifted = _windows_in_audio_time(request(windows=[TimeRange(start=0.2, end=3.0)]))
    assert shifted[0].start == 0.0


def test_the_out_of_window_filter_uses_audio_time():
    """The filter runs on raw model output, i.e. before the offset is added back.

    Comparing wav-time words against container-time windows drops every word in
    a file with a non-zero start_time once the error exceeds the tolerance.
    """
    raw = [
        {
            "start": 10.0,
            "end": 15.0,
            "text": "hello",
            # wav time: this is container 11.0, comfortably inside the window
            "words": [{"word": "hello", "start": 10.5, "end": 10.9}],
        }
    ]
    segments, dropped = WhisperTranscriber._to_segments(raw, request())
    assert dropped == 0
    assert segments[0].words[0].start == 10.5 + OFFSET


def test_persisted_windows_stay_in_container_time():
    """``transcript.json`` must obey the same clock as every other artifact."""
    windows = [TimeRange(start=10.5, end=15.5)]
    transcript = WhisperTranscriber._build_transcript(
        request(windows=windows), segments=[], language="en", align_model=None, dropped=0
    )
    assert [(w.start, w.end) for w in transcript.windows] == [(10.5, 15.5)]
