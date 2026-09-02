"""Fake `extract`: a token wav, so the pruner has something to reclaim."""

from __future__ import annotations

from tests.support.stages import CONTROL

NAME = "extract"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    ctx.ws.audio_wav.write_bytes(b"RIFF" + b"\0" * 1024)
    ctx.progress(NAME, 1.0)


def load(ws):
    return ws.audio_wav if ws.audio_wav.is_file() else None
