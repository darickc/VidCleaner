"""Fake `transcribe`."""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.artifacts import (
    TimeRange,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)

NAME = "transcribe"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    ctx.progress(NAME, 0.5)
    Transcript(
        mode=CONTROL.extras.get("mode", "windowed"),
        mode_reason=CONTROL.extras.get("mode_reason", ""),
        model="fake-model",
        windows=[TimeRange(start=0.0, end=3.5)],
        segments=[
            TranscriptSegment(
                start=1.0,
                end=2.0,
                text="Oh shit.",
                words=[
                    TranscriptWord(word="Oh", start=1.0, end=1.1),
                    TranscriptWord(word="shit", start=1.2, end=1.5),
                ],
            )
        ],
    ).write(ctx.ws.transcript_json)
    ctx.progress(NAME, 1.0)


def load(ws):
    return Transcript.read(ws.transcript_json) if ws.transcript_json.is_file() else None
