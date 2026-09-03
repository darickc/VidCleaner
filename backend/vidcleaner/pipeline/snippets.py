"""Review clips for every detection -- §6 step 10.

Two five-second audio clips per detection, cut from ``audio.wav``: ``orig.m4a`` as the
source sounded and ``clean.m4a`` with this detection's mute applied, plus a
``wave.png`` waveform with the muted range highlighted. Audio only, per §11 ("video
previews are a later option"): a clip is ~40 kB, so a movie's worth is a few MB.

**Non-fatal, like `refresh`.** By the time this runs the swap has committed and the
library file is correct; a missing clip costs the user a play button, not a bad file.
Every failure is a warning on the result.

**Not a pure function of ``/work``.** The clips are written to
``Settings.snippets_dir`` (under ``/config``), because ``/work`` is reclaimed a week
after a job finishes while the detections they illustrate live in the database
forever. Resume safety comes from idempotence instead: the same inputs produce the
same files at the same paths, and re-running only overwrites them.

ONE CLOCK (see ``artifacts.py``): detections are in **source container time** and
``audio.wav`` is 0-based, so cutting from it subtracts
``probe.source_audio.start_time``. This is the third and last module allowed to do
that arithmetic.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import (
    DetectionResult,
    ProbeResult,
    Snippet,
    SnippetsResult,
)
from vidcleaner.pipeline.workspace import Workspace

__all__ = ["MAX_SNIPPETS", "NAME", "WINDOW_S", "clip_window", "load", "run", "snippet_graph"]

NAME = "snippets"
log = get_logger(__name__)

#: §6 step 10's "5 s audio clips ... centred on the mute".
WINDOW_S: Final = 5.0
#: A pathological file (a stand-up special, an audit pass over a war film) can carry
#: hundreds of hits, and each one costs an ffmpeg start. Beyond this the rest are
#: recorded as skipped rather than silently missing.
MAX_SNIPPETS: Final = 300
#: Waveform PNG size, and the highlight drawn over the muted span.
WAVE_SIZE: Final = (640, 120)

FILES: Final = ("orig.m4a", "clean.m4a", "wave.png")


def clip_window(
    mute_start_s: float,
    mute_end_s: float,
    *,
    audio_offset_s: float = 0.0,
    duration_s: float | None = None,
) -> tuple[float, float, float, float]:
    """Where to cut, in ``audio.wav`` time, and where the mute sits inside the clip.

    Returns ``(clip_start, clip_duration, rel_mute_start, rel_mute_end)``. The window
    is centred on the mute and clamped to the file, so a hit in the first two seconds
    still yields five seconds of context -- it just is not centred.
    """
    start = max(0.0, mute_start_s - audio_offset_s)
    end = max(start, mute_end_s - audio_offset_s)
    length = max(WINDOW_S, end - start)

    clip_start = (start + end) / 2.0 - length / 2.0
    if duration_s is not None:
        clip_start = min(clip_start, max(0.0, duration_s - length))
    clip_start = max(0.0, clip_start)
    if duration_s is not None:
        length = min(length, max(0.001, duration_s - clip_start))
    return clip_start, length, start - clip_start, end - clip_start


def snippet_graph(rel_start: float, rel_end: float, duration: float) -> str:
    """The filter graph for one snippet: original, muted, and a highlighted waveform.

    ``asetpts=PTS-STARTPTS`` is load-bearing. The clip is seeked input-side for
    speed, and without a reset the ``volume`` filter's ``enable`` would be evaluated
    against timestamps whose origin depends on how ffmpeg handled the seek.
    """
    width, height = WAVE_SIZE
    span = max(duration, 0.001)
    box_x = max(0, min(width - 1, int(round(rel_start / span * width))))
    box_w = max(2, min(width - box_x, int(round((rel_end - rel_start) / span * width))))
    return "\n".join(
        (
            "[0:a:0]asetpts=PTS-STARTPTS,asplit=3[orig][tomute][towave];",
            # asetnsamples matches render.py: `enable` gates whole frames, so a
            # 21 ms AAC frame would otherwise round the mute outward by a frame.
            f"[tomute]asetnsamples=n=240,volume=0:enable='between(t,{rel_start:.3f},"
            f"{rel_end:.3f})'[clean];",
            f"[towave]showwavespic=s={width}x{height}:colors=#38bdf8,format=rgba,"
            f"drawbox=x={box_x}:y=0:w={box_w}:h={height}:color=#f43f5e@0.35:t=fill[wave]",
        )
    )


def snippet_root(ctx) -> Path:
    """``<config>/snippets/<job id>`` -- always, CLI runs included.

    Keying this on "is there a database row for this job" was the first design and
    it is the wrong seam: the CLI persists its detections too, so its clips are just
    as much the Item page's evidence, and a rule that quietly changes where output
    lands is the kind of thing nobody remembers when reading a bug report.
    """
    return ctx.deploy.snippets_dir / ctx.spec.job_id


def run(ctx) -> None:
    started = time.monotonic()
    ws: Workspace = ctx.ws
    warnings: list[str] = []
    skipped: list[str] = []
    made: list[Snippet] = []

    detections = DetectionResult.read(ws.detections_json) if ws.detections_json.is_file() else None
    probe = ProbeResult.read(ws.probe_json) if ws.probe_json.is_file() else None

    if detections is None or not detections.detections:
        skipped.append("no_detections")
    elif not ws.audio_wav.is_file():
        # A resumed job whose work dir was pruned. Nothing is wrong with the library
        # file; the user just cannot audition this run.
        skipped.append("no_audio")
    else:
        root = snippet_root(ctx)
        offset = probe.source_audio.start_time if probe else 0.0
        duration = probe.duration if probe else None
        wanted = detections.detections[:MAX_SNIPPETS]
        if len(detections.detections) > MAX_SNIPPETS:
            skipped.append(f"capped_at_{MAX_SNIPPETS}")

        for index, detection in enumerate(wanted):
            try:
                made.append(
                    _one(
                        ctx,
                        root=root,
                        index=index,
                        detection=detection,
                        offset=offset,
                        duration=duration,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one bad clip is not a bad job
                warnings.append(f"detection {index} ({detection.word_canonical}): {exc}")
            ctx.progress(NAME, (index + 1) / len(wanted))

    result = SnippetsResult(
        root=str(snippet_root(ctx)),
        snippets=made,
        skipped=skipped,
        warnings=warnings,
        elapsed_s=round(time.monotonic() - started, 3),
    )
    result.write(ws.snippets_json)
    ctx.progress(NAME, 1.0)
    log.info(
        "snippets.done",
        job_id=ctx.spec.job_id,
        made=len(made),
        skipped=skipped,
        warnings=len(warnings),
    )


def _one(
    ctx, *, root: Path, index: int, detection, offset: float, duration: float | None
) -> Snippet:
    clip_start, clip_len, rel_start, rel_end = clip_window(
        detection.mute_start_s,
        detection.mute_end_s,
        audio_offset_s=offset,
        duration_s=(duration - offset) if duration is not None else None,
    )
    rel_dir = f"{index:04d}"
    out_dir = root / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx.runner.run_filtered(
        input_args=[
            "-ss",
            f"{clip_start:.3f}",
            "-t",
            f"{clip_len:.3f}",
            "-i",
            str(ctx.ws.audio_wav),
        ],
        graph=snippet_graph(rel_start, rel_end, clip_len),
        # One graph file per job, rewritten per clip: the snippets never resume
        # mid-way, so there is nothing to preserve between them.
        graph_path=ctx.ws.path("snippet.graph.txt"),
        output_args=[
            "-map",
            "[orig]",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(out_dir / "orig.m4a"),
            "-map",
            "[clean]",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(out_dir / "clean.m4a"),
            "-map",
            "[wave]",
            "-frames:v",
            "1",
            str(out_dir / "wave.png"),
        ],
        label=f"snippet:{index:04d}",
        loglevel="error",
    )
    return Snippet(
        detection_index=index,
        rel_dir=rel_dir,
        word_canonical=detection.word_canonical,
        # Reported in container time, the clock every other stored time uses.
        clip_start_s=round(clip_start + offset, 3),
        clip_duration_s=round(clip_len, 3),
        mute_start_s=detection.mute_start_s,
        mute_end_s=detection.mute_end_s,
        files=[name for name in FILES if (out_dir / name).is_file()],
    )


def load(ws: Workspace) -> SnippetsResult | None:
    return SnippetsResult.read(ws.snippets_json) if ws.snippets_json.is_file() else None
