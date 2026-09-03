"""§6's "path vanished": ask the arr where the file went.

§6 step "Path vanished / stale" says: *"if the source path disappears mid-job,
re-resolve via the arr API (`/episodefile/{id}`), requeue once, else `stale`"*.
`policy.classify` has implemented the requeue since M3 and `stages.StaleSourceError`'s
docstring has described the re-resolution -- but **nothing re-resolved anything**, so
the retry ran against the identical path sixty seconds later and could only fail the
same way. The one allowed retry was therefore guaranteed to be wasted.

The transport was already there and unused for this: `SonarrClient.get_episode_file`
even says "``None`` on 404 rather than an exception: §6's 'path vanished' path calls
this precisely because the file may be gone". This module is that caller.

Kept out of `runner.py` because it is a pure decision over three inputs (the item, the
arr's answer, the path map) and belongs where it can be tested without a worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from vidcleaner.db.models import MediaItem, Title
from vidcleaner.logging import get_logger

__all__ = ["Resolution", "reresolve_path"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Resolution:
    outcome: str
    """``moved`` (the path changed and was written), ``unchanged`` (the arr still says
    the same path), ``gone`` (the arr has no such file), or ``unavailable`` (no client,
    no ``arr_file_id``, or the call failed)."""
    path: str | None = None
    detail: str = ""

    @property
    def worth_retrying(self) -> bool:
        """Only a *moved* file justifies spending the retry.

        This is the point of the module. A retry against an unchanged path re-raises
        `StaleSourceError` for exactly the same reason, so it buys nothing but a
        minute's delay and a burned attempt -- and `attempts` counts claims, so a
        crash could have spent the allowance already.
        """
        return self.outcome == "moved"


def reresolve_path(
    session: Session, item: MediaItem, title: Title | None, integrations: Any
) -> Resolution:
    """Ask the arr for this item's current path and write it if it has changed.

    Deliberately keyed on ``arr_file_id`` rather than on a path search: it is the
    identity M3 settled on (a `Rename` webhook is keyed the same way), and it survives
    exactly the folder moves and renames that cause this failure in the first place.
    """
    if integrations is None:
        return Resolution("unavailable", detail="no integrations configured")
    if item.arr_file_id is None:
        # A CLI run's row, or a file the arr never told us about. Nothing to ask.
        return Resolution("unavailable", detail="no arr file id")
    if title is None or title.arr_id is None or title.arr_id < 0:
        return Resolution("unavailable", detail="not an arr-backed title")

    if title.kind == "series":
        client, pathmap = integrations.sonarr, integrations.sonarr_map
        fetch = "get_episode_file"
    else:
        client, pathmap = integrations.radarr, integrations.radarr_map
        fetch = "get_movie_file"
    if client is None:
        return Resolution("unavailable", detail=f"no {title.kind} client configured")

    try:
        found = getattr(client, fetch)(item.arr_file_id)
    except Exception as exc:  # noqa: BLE001 - a hung arr must not mask the real failure
        return Resolution("unavailable", detail=f"{type(exc).__name__}: {exc}"[:200])

    if found is None or not found.path:
        # §6's "else stale". The arr agrees the file is gone, which is the most
        # useful thing it can tell us: no amount of retrying will bring it back.
        return Resolution("gone", detail="the arr no longer has this file")

    local = pathmap.to_local(found.path) if pathmap is not None else found.path
    if local == item.path:
        return Resolution("unchanged", path=local, detail="the arr still reports this path")

    was = item.path
    item.path = local
    session.flush()
    log.info("resolve.moved", media_item_id=item.id, was=was, now=local)
    return Resolution("moved", path=local, detail=f"moved from {was}")
