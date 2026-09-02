"""Fake `subtitles`: one cue, one hit, one window."""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.artifacts import (
    SubtitleCue,
    SubtitleHit,
    SubtitleSource,
    SubtitlesResult,
    TimeRange,
)

NAME = "subtitles"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    has_subs = CONTROL.extras.get("subtitles", True)
    SubtitlesResult(
        source=SubtitleSource(kind="embedded", reason="embedded_preferred_language")
        if has_subs
        else SubtitleSource(kind="none", reason="no_subtitles"),
        cues=[SubtitleCue(index=0, start=1.0, end=2.0, text="Oh shit.")] if has_subs else [],
        hits=(
            [
                SubtitleHit(
                    cue_index=0,
                    start=1.2,
                    end=1.5,
                    word_raw="shit",
                    word_canonical="shit",
                    category="strong",
                    char_start=3,
                    char_end=7,
                )
            ]
            if has_subs
            else []
        ),
        windows=[TimeRange(start=0.0, end=3.5)] if has_subs else [],
        redactable=[0] if has_subs else [],
    ).write(ctx.ws.subs_json)


def load(ws):
    return SubtitlesResult.read(ws.subs_json) if ws.subs_json.is_file() else None
