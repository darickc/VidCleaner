"""Fake `probe`: a plausible ProbeResult, with `already_clean` switchable."""

from __future__ import annotations

from pathlib import Path

from tests.support.stages import CONTROL
from vidcleaner.pipeline.artifacts import AudioStreamInfo, CodecPlan, ProbeResult

NAME = "probe"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    source = Path(ctx.spec.source_path)
    stat = source.stat() if source.is_file() else None
    ProbeResult(
        path=str(source),
        size=stat.st_size if stat else CONTROL.source_size,
        mtime=stat.st_mtime if stat else 1.0,
        inode=stat.st_ino if stat else None,
        container_format="matroska",
        duration=600.0,
        fingerprint="fp-test",
        audio=[
            AudioStreamInfo(index=1, typed_index=0, codec_name="eac3", channels=6, language="eng")
        ],
        clean_codec=CodecPlan(encoder="eac3", reason="test"),
        already_clean=CONTROL.already_clean,
    ).write(ctx.ws.probe_json)


def load(ws):
    return ProbeResult.read(ws.probe_json) if ws.probe_json.is_file() else None
