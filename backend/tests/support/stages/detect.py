"""Fake `detect`."""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.artifacts import Detection, DetectionResult, TimeRange

NAME = "detect"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    detections = [
        Detection(
            word_raw="shit",
            word_canonical="shit",
            category="strong",
            start_s=1.2 + n,
            end_s=1.5 + n,
            mute_start_s=1.1 + n,
            mute_end_s=1.6 + n,
            source="both",
            confidence=0.9,
        )
        for n in range(CONTROL.detections)
    ]
    DetectionResult(
        profile_hash=ctx.spec.profile_hash,
        detections=detections,
        mute_ranges=[TimeRange(start=d.mute_start_s, end=d.mute_end_s) for d in detections],
        total_muted_s=sum(d.mute_end_s - d.mute_start_s for d in detections),
    ).write(ctx.ws.detections_json)


def load(ws):
    return DetectionResult.read(ws.detections_json) if ws.detections_json.is_file() else None
