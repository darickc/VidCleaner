"""Queries shared by the api and the worker.

`api/views.py` owned :func:`evidence_job_ids` through M4, when only the read API
needed it. M5's audit pass needs the same answer -- "which run describes the file on
disk?" -- from inside the worker, and a worker that imports `vidcleaner.api` to get it
is a layering the next reader would rightly undo. So the query lives here and
`api/views.py` re-exports it.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.models import Job, MediaItem

__all__ = ["EVIDENCE_STATES", "evidence_job_ids"]


#: States a job can be in and still have something to say about what is in the file.
#: ``already_clean`` is the interesting exclusion: that job short-circuited at `probe`
#: and never reached `detect`.
EVIDENCE_STATES: tuple[str, ...] = ("done", "failed", "stale")


def evidence_job_ids(session: Session, items: Sequence[MediaItem]) -> dict[int, str]:
    """item id -> the job whose detections describe that file today.

    Usually ``media_items.last_job_id``, which is what §5's rollup query names. But
    the last *run* is not always the last run that describes **the file on disk**, and
    reading the wrong one blanks the Item page and the Title rollup for an episode that
    is in fact full of muted words. Two ways that happens:

    * §4's idempotency check ends ``already_clean`` without reaching `detect`, and it
      does update ``last_job_id`` -- it must, because the profile hash it records is
      what stops the hourly sync re-enqueueing the file forever. Found by the M4 demo.
    * **A dry run** reaches `detect` and writes a full set of detections, but changes
      nothing on disk. M5's audit phase 1 is a dry run by construction, and so is the
      "dry run" button on a cleaned episode -- which, since it forces past
      ``already_clean``, detects over the muted Clean track and would otherwise become
      the evidence for a file it never touched.

    So the test is "reached `detect` **and** was not a dry run", which covers both.
    A reprocess that legitimately finds nothing is not a dry run and still wins over an
    older run that found something.
    """
    ids = [item.id for item in items]
    if not ids:
        return {}
    rows = session.execute(
        select(Job.id, Job.media_item_id, Job.state, Job.dry_run)
        .where(Job.media_item_id.in_(ids))
        .order_by(Job.created_at.desc(), Job.id.desc())
    ).all()

    describes = {
        job_id: state in EVIDENCE_STATES and not dry_run for job_id, _, state, dry_run in rows
    }
    newest_with_evidence: dict[int, str] = {}
    for job_id, item_id, _state, _dry in rows:
        if describes[job_id] and item_id not in newest_with_evidence:
            newest_with_evidence[item_id] = job_id

    chosen: dict[int, str] = {}
    for item in items:
        last = item.last_job_id
        if last is not None and describes.get(last):
            chosen[item.id] = last
        elif item.id in newest_with_evidence:
            chosen[item.id] = newest_with_evidence[item.id]
        elif last is not None:
            chosen[item.id] = last
    return chosen
