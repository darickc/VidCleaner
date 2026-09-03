"""Fake `snippets`: writes the artifact without starting ffmpeg.

The file layout is real (a directory per detection under the deployment's snippet
root), because the API's media endpoint resolves those paths.
"""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.artifacts import DetectionResult, Snippet, SnippetsResult
from vidcleaner.pipeline.snippets import FILES, snippet_root

NAME = "snippets"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    root = snippet_root(ctx)
    detections = (
        DetectionResult.read(ctx.ws.detections_json).detections
        if ctx.ws.detections_json.is_file()
        else []
    )
    made = []
    for index, detection in enumerate(detections):
        out_dir = root / f"{index:04d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            (out_dir / name).write_bytes(b"\0" * 64)
        made.append(
            Snippet(
                detection_index=index,
                rel_dir=f"{index:04d}",
                word_canonical=detection.word_canonical,
                clip_start_s=max(0.0, detection.mute_start_s - 2.5),
                clip_duration_s=5.0,
                mute_start_s=detection.mute_start_s,
                mute_end_s=detection.mute_end_s,
                files=list(FILES),
            )
        )
    SnippetsResult(root=str(root), snippets=made).write(ctx.ws.snippets_json)
    ctx.progress(NAME, 1.0)


def load(ws):
    return SnippetsResult.read(ws.snippets_json) if ws.snippets_json.is_file() else None
