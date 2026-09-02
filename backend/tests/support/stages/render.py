"""Fake `render`: writes an `out.mkv` for `swap` to move."""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.artifacts import RenderResult

NAME = "render"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    ctx.progress(NAME, 0.5)
    ctx.ws.out_mkv.write_bytes(b"c" * CONTROL.out_size)
    ctx.ws.graph_txt.write_text("volume=0\n")
    RenderResult(
        out_path=str(ctx.ws.out_mkv),
        size=CONTROL.out_size,
        encoder="eac3",
        mute_range_count=CONTROL.detections,
        tags={"VIDCLEANER_JOB": ctx.spec.job_id},
    ).write(ctx.ws.render_json)
    ctx.progress(NAME, 1.0)


def load(ws):
    return RenderResult.read(ws.render_json) if ws.render_json.is_file() else None
