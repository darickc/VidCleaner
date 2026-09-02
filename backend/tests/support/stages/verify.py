"""Fake `verify`."""

from __future__ import annotations

from tests.support.stages import CONTROL
from vidcleaner.pipeline.artifacts import Check, VerifyResult

NAME = "verify"


def run(ctx) -> None:
    CONTROL.enter(NAME)
    result = VerifyResult(
        ok=CONTROL.verify_ok,
        checks=[Check(name="fake", ok=CONTROL.verify_ok, detail="test")],
        measured_db=[-90.0],
    )
    result.write(ctx.ws.verify_json)
    if not result.ok:
        # The real stage raises after writing, so `swap` can see why.
        raise RuntimeError("verification failed: fake")


def load(ws):
    return VerifyResult.read(ws.verify_json) if ws.verify_json.is_file() else None
