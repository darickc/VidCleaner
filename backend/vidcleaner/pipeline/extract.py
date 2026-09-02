"""Stage 2: decode the source audio to a 16 kHz mono WAV (PLAN.md §6 step 2).

One extraction feeds STT, the M2 drift check and the M4 snippet generator.
16 kHz mono is what Whisper and wav2vec2 both consume, so resampling here avoids
doing it three times.

**Clock note.** ffmpeg does not pad the output with the input stream's
``start_time``, so ``audio.wav`` begins at the stream's first sample and

    wav_time = container_time - source_audio.start_time

Nothing here compensates for that: ``stt`` adds the offset back exactly once,
recording it in ``Transcript.audio_start_offset_s``. See the ONE CLOCK note in
``artifacts.py``.
"""

from __future__ import annotations

from vidcleaner.pipeline.artifacts import ProbeResult
from vidcleaner.pipeline.workspace import Workspace

NAME = "extract"

__all__ = ["NAME", "SAMPLE_RATE", "load", "run"]

SAMPLE_RATE = 16_000


def run(ctx) -> None:
    probe = ProbeResult.read(ctx.ws.probe_json)
    source = probe.source_audio
    target = ctx.ws.audio_wav

    ctx.runner.run(
        [
            "-i",
            probe.path,
            "-map",
            f"0:a:{source.typed_index}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(target),
        ],
        label="extract-audio",
        on_progress=lambda p: ctx.progress(NAME, p.fraction or 0.0),
        total_duration=probe.duration or None,
        timeout=max(600.0, (probe.duration or 0) * 2),
    )

    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(f"extraction produced no audio at {target}")

    ctx.log.info(
        "extract.done",
        stream=f"a:{source.typed_index}",
        start_time=source.start_time,
        bytes=target.stat().st_size,
    )


def load(ws: Workspace):
    """No parsed artifact -- the WAV itself is the output."""
    return ws.audio_wav if ws.audio_wav.is_file() else None
