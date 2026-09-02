"""Telling Sonarr/Radarr and Jellyfin that the file changed -- §6 step 9.

**Non-fatal by design.** By the time this runs, `swap` has committed and the library
file is correct. A failed rescan costs a stale entry until the hourly sync catches up,
so every client error is a warning on the result rather than a `StageError`. That is
also what makes re-running the stage safe, which matters because it is the only way a
resumed job can reach it.

Like `swap`, this is **not** a pure function of its on-disk inputs -- it talks to the
network. CLAUDE.md's contract exists to make resume safe, and here that property comes
from idempotence instead: a rescan command and a path-changed notification are both
harmless twice.

The credentials do not come from `job.json`: `build_spec` strips `SECRET_FIELDS`
because `/work` ends up in bug reports. They arrive through
`StageContext.integrations`, while `JobSpec.target` carries the ids.
"""

from __future__ import annotations

import time
from typing import Any

from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import RefreshResult, SwapResult
from vidcleaner.pipeline.workspace import Workspace

__all__ = ["NAME", "load", "plan_updates", "run"]

NAME = "refresh"
log = get_logger(__name__)


def plan_updates(swap: SwapResult, pathmap: Any) -> list[dict[str, str]]:
    """What to tell Jellyfin, in one call.

    §6 remembers the ``Deleted`` for an old ``.mp4`` name but leaves the new one as
    ``Modified``. That is wrong: after an MP4 to MKV swap the ``.mkv`` is a path
    Jellyfin has never seen, so it is **`Created`**.
    """
    to_remote = getattr(pathmap, "to_remote", None) or (lambda p: p)
    updates = [
        {
            "Path": to_remote(swap.final_path),
            "UpdateType": "Created" if swap.old_path else "Modified",
        }
    ]
    if swap.old_path:
        updates.append({"Path": to_remote(swap.old_path), "UpdateType": "Deleted"})
    return updates


def run(ctx) -> None:
    from vidcleaner.integrations.jellyfin import MediaUpdate  # noqa: PLC0415

    started = time.monotonic()
    swap = SwapResult.read(ctx.ws.swap_json)
    target = ctx.spec.target
    integrations = ctx.integrations

    warnings: list[str] = []
    skipped: list[str] = []
    arr_name: str | None = None
    command_id: int | None = None
    command_name: str | None = None

    if integrations is None:
        skipped.append("no_integrations")
    else:
        # --- the arr rescan. §3: a same-path replacement is re-analyzed only if the
        # byte size changed, which adding a track guarantees -- but an explicit
        # rescan is what makes it prompt rather than eventual.
        client = integrations.arr(target.arr_app if target else None)
        if target is None or target.arr_app is None or target.arr_id is None:
            skipped.append("no_arr_target")
        elif client is None:
            skipped.append(f"{target.arr_app}_not_configured")
        else:
            arr_name = target.arr_app
            try:
                status = client.rescan(target.arr_id)
                command_id, command_name = status.id, status.name
            except Exception as exc:  # noqa: BLE001 - the library is already correct
                warnings.append(f"{target.arr_app} rescan failed: {exc}")

        # --- Jellyfin, one call carrying every path that changed
        if integrations.jellyfin is None:
            skipped.append("jellyfin_not_configured")
        else:
            updates = plan_updates(swap, integrations.map_for("jellyfin"))
            try:
                integrations.jellyfin.media_updated(
                    [MediaUpdate.model_validate(u) for u in updates]
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"jellyfin refresh failed: {exc}")

    result = RefreshResult(
        arr=arr_name,  # type: ignore[arg-type]
        arr_command_id=command_id,
        arr_command_name=command_name,
        jellyfin_updates=(
            plan_updates(swap, integrations.map_for("jellyfin")) if integrations else []
        ),
        warnings=warnings,
        skipped=skipped,
        elapsed_s=round(time.monotonic() - started, 3),
    )
    result.write(ctx.ws.refresh_json)
    ctx.progress(NAME, 1.0)
    log.info(
        "refresh.done",
        job_id=ctx.spec.job_id,
        arr=arr_name,
        command=command_id,
        updates=len(result.jellyfin_updates),
        skipped=skipped,
        warnings=warnings,
    )


def load(ws: Workspace) -> RefreshResult | None:
    return RefreshResult.read(ws.refresh_json) if ws.refresh_json.is_file() else None
