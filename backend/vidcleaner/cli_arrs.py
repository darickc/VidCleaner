"""``vidcleaner integrations|sync|titles|queue`` -- the M3 controls.

PLAN.md §9 puts all of this behind a UI, which is M4. These exist so the milestone's
behaviour is demonstrable (and scriptable on the unraid box) before the screens do,
and so M4 is free to design its API around the actual screens rather than inheriting
whatever shape was convenient now.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

__all__ = ["add_parsers", "dispatch"]


def add_parsers(subparsers: argparse._SubParsersAction) -> None:
    integrations = subparsers.add_parser(
        "integrations", help="check the Sonarr/Radarr/Jellyfin connections"
    )
    integrations.add_argument("action", nargs="?", default="test", choices=("test",))
    integrations.add_argument("--json", action="store_true", dest="as_json")

    sync = subparsers.add_parser(
        "sync", help="pull titles and files from the arrs and enqueue what needs cleaning"
    )
    sync.add_argument(
        "--no-enqueue", action="store_true", help="update the library only, queue nothing"
    )
    sync.add_argument(
        "--confirm",
        action="store_true",
        help="also re-check that the arrs agree with our paths for recently cleaned files",
    )
    sync.add_argument("--json", action="store_true", dest="as_json")

    titles = subparsers.add_parser("titles", help="list, enable or disable series and movies")
    titles.add_argument("action", nargs="?", default="list", choices=("list", "enable", "disable"))
    titles.add_argument("--kind", choices=("series", "movie"), help="filter or target a kind")
    titles.add_argument("--arr-id", type=int, help="the Sonarr series id or Radarr movie id")
    titles.add_argument("--name", help="match a title by name instead of id (must be unique)")
    titles.add_argument("--enabled", action="store_true", help="list only enabled titles")
    titles.add_argument(
        "--all",
        action="store_true",
        help="enable: also queue the episodes the series already has (pre-M7 behaviour)",
    )

    queue = subparsers.add_parser("queue", help="inspect and steer the job queue")
    queue.add_argument(
        "action", nargs="?", default="list", choices=("list", "show", "cancel", "retry")
    )
    queue.add_argument("job_id", nargs="?", help="for show, cancel and retry")


def dispatch(args: argparse.Namespace) -> int:
    from vidcleaner.logging import configure_logging  # noqa: PLC0415

    configure_logging("WARNING" if getattr(args, "as_json", False) else "INFO", role="cli")
    if args.command == "integrations":
        return _integrations(as_json=args.as_json)
    if args.command == "sync":
        return _sync(enqueue=not args.no_enqueue, confirm=args.confirm, as_json=args.as_json)
    if args.command == "titles":
        return _titles(args)
    if args.command == "queue":
        return _queue(args)
    return 1  # pragma: no cover - argparse constrains the choices


# ------------------------------------------------------------- integrations


def _integrations(*, as_json: bool) -> int:
    from vidcleaner.db.session import session_scope  # noqa: PLC0415
    from vidcleaner.integrations import from_database  # noqa: PLC0415

    with session_scope() as session:
        bundle = from_database(session)
    results = []
    try:
        for app, client in (
            ("sonarr", bundle.sonarr),
            ("radarr", bundle.radarr),
            ("jellyfin", bundle.jellyfin),
        ):
            if client is None:
                results.append({"app": app, "ok": None, "detail": "not configured"})
                continue
            outcome = client.test()
            results.append(
                {
                    "app": app,
                    "ok": outcome.ok,
                    "version": outcome.version,
                    "detail": outcome.detail,
                    "latency_ms": outcome.latency_ms,
                }
            )
    finally:
        bundle.close()

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        print(f"{'APP':10} {'STATUS':8} {'VERSION':16} DETAIL")
        for row in results:
            status = "-" if row["ok"] is None else ("ok" if row["ok"] else "FAILED")
            print(f"{row['app']:10} {status:8} {(row.get('version') or ''):16} {row['detail']}")
    return 0 if all(r["ok"] is not False for r in results) else 1


# --------------------------------------------------------------------- sync


def _sync(*, enqueue: bool, confirm: bool, as_json: bool) -> int:
    from vidcleaner.db.session import session_scope  # noqa: PLC0415
    from vidcleaner.integrations import from_database  # noqa: PLC0415
    from vidcleaner.integrations.sync import sync_all  # noqa: PLC0415
    from vidcleaner.settings_store import load_settings  # noqa: PLC0415

    with session_scope() as session:
        bundle = from_database(session)
        settings = load_settings(session)
    if bundle.sonarr is None and bundle.radarr is None:
        print("error: neither Sonarr nor Radarr is configured", file=sys.stderr)
        return 78
    try:
        report = sync_all(
            integrations=bundle,
            settings=settings,
            enqueue_backfill=enqueue,
            confirm=confirm,
        )
    finally:
        bundle.close()

    if as_json:
        print(json.dumps(vars(report), indent=2))
    else:
        print(
            f"Titles     {report.titles_seen} seen, {report.titles_added} added, "
            f"{report.titles_missing} no longer listed"
        )
        print(
            f"Items      {report.items_seen} seen, {report.items_added} added, "
            f"{report.items_adopted} adopted, {report.items_merged} merged, "
            f"{report.items_stale} stale"
        )
        print(f"Queued     {len(report.enqueued)} job(s)")
        if confirm:
            print(
                f"Mapping    {report.mapping_checked} checked, "
                f"{report.mapping_mismatched} mismatched"
            )
        for error in report.errors:
            print(f"           error: {error}", file=sys.stderr)
    return 1 if report.errors else 0


# ------------------------------------------------------------------- titles


def _titles(args: argparse.Namespace) -> int:
    from sqlalchemy import select  # noqa: PLC0415

    from vidcleaner.db.models import MediaItem, Title  # noqa: PLC0415
    from vidcleaner.db.session import session_scope  # noqa: PLC0415

    if args.action == "list":
        with session_scope() as session:
            query = select(Title).where(Title.arr_id >= 0)
            if args.kind:
                query = query.where(Title.kind == args.kind)
            if args.enabled:
                query = query.where(Title.enabled.is_(True))
            titles = session.scalars(query.order_by(Title.kind, Title.title)).all()
            if not titles:
                print("no titles; run `vidcleaner sync` first")
                return 0
            print(f"{'KIND':8} {'ARR':>6}  {'CLEAN':>9}  ON   TITLE")
            for title in titles:
                items = session.scalars(
                    select(MediaItem).where(MediaItem.title_id == title.id)
                ).all()
                clean = sum(1 for i in items if i.status in ("clean", "already_clean"))
                mark = "yes" if title.enabled else " - "
                print(
                    f"{title.kind:8} {title.arr_id:>6}  {clean:>4}/{len(items):<4}  "
                    f"{mark}  {title.title}"
                )
        return 0

    return _toggle_title(args, enable=args.action == "enable")


def _toggle_title(args: argparse.Namespace, *, enable: bool) -> int:
    from sqlalchemy import select  # noqa: PLC0415

    from vidcleaner.db.models import Title  # noqa: PLC0415
    from vidcleaner.db.session import session_scope, utcnow  # noqa: PLC0415
    from vidcleaner.integrations import from_database  # noqa: PLC0415
    from vidcleaner.integrations.sync import (  # noqa: PLC0415
        backfill_title,
        defer_existing_items,
        sync_title_items,
    )

    if args.arr_id is None and not args.name:
        print("error: pass --arr-id or --name", file=sys.stderr)
        return 64

    with session_scope() as session:
        query = select(Title).where(Title.arr_id >= 0)
        if args.kind:
            query = query.where(Title.kind == args.kind)
        if args.arr_id is not None:
            query = query.where(Title.arr_id == args.arr_id)
        if args.name:
            query = query.where(Title.title.ilike(f"%{args.name}%"))
        matches = session.scalars(query).all()
        if not matches:
            print("error: no matching title", file=sys.stderr)
            return 66
        if len(matches) > 1:
            print("error: matches several titles:", file=sys.stderr)
            for title in matches:
                print(f"  {title.kind} {title.arr_id}  {title.title}", file=sys.stderr)
            return 64
        title = matches[0]
        title.enabled = enable
        deferred = 0
        if enable and title.kind == "series" and not args.all:
            # Same rule as the UI's toggle (§2 as amended in M7): the episodes the
            # series already has arrive unselected. `--all` is the old behaviour.
            title.backfill_from = utcnow()
            session.flush()
            deferred = defer_existing_items(session, title)
        title_id, kind, name = title.id, title.kind, title.title

    if not enable:
        print(f"Disabled   {kind} {name}")
        return 0

    # §8: "Enabling a title triggers immediate backfill of its files."
    with session_scope() as session:
        bundle = from_database(session)
        title = session.get(Title, title_id)
        client = bundle.arr("sonarr" if kind == "series" else "radarr")
        if client is None or title is None:
            bundle.close()
            print(f"Enabled    {kind} {name} (no arr configured; nothing to backfill)")
            if deferred:
                print(f"Deferred   {deferred} existing file(s)")
            return 0
        pathmap = bundle.map_for("sonarr" if kind == "series" else "radarr")
        try:
            report = sync_title_items(session, title, client=client, pathmap=pathmap)
            deferred += report.items_deferred
            queued = backfill_title(session, title, client=client, pathmap=pathmap)
        finally:
            bundle.close()

    print(f"Enabled    {kind} {name}")
    print(f"Queued     {len(queued)} job(s)")
    if deferred:
        print(f"Deferred   {deferred} existing file(s); select them in the UI or pass --all")
    return 0


# -------------------------------------------------------------------- queue


def _queue(args: argparse.Namespace) -> int:
    from sqlalchemy import select  # noqa: PLC0415

    from vidcleaner.db.models import Job, JobLog, MediaItem  # noqa: PLC0415
    from vidcleaner.db.session import session_scope  # noqa: PLC0415
    from vidcleaner.worker.claim import cancel, release  # noqa: PLC0415

    if args.action in ("cancel", "retry") and not args.job_id:
        print(f"error: {args.action} needs a job id", file=sys.stderr)
        return 64

    with session_scope() as session:
        if args.action == "cancel":
            ok = cancel(session, args.job_id, reason="cli")
            print("cancelled" if ok else "not cancellable (already finished?)")
            return 0 if ok else 1
        if args.action == "retry":
            ok = release(session, job_id=args.job_id, retry_at=None, error=None)
            print("requeued" if ok else "no such job")
            return 0 if ok else 1
        if args.action == "show":
            job = session.get(Job, args.job_id) if args.job_id else None
            if job is None:
                print("error: no such job", file=sys.stderr)
                return 66
            item = session.get(MediaItem, job.media_item_id)
            print(json.dumps(_job_row(job, item), indent=2))
            for row in session.scalars(
                select(JobLog).where(JobLog.job_id == job.id).order_by(JobLog.id)
            ):
                print(f"  {row.ts:%H:%M:%S} {row.level:8} {row.msg}")
            return 0

        jobs = session.scalars(select(Job).order_by(Job.created_at.desc()).limit(50)).all()
        if not jobs:
            print("the queue is empty")
            return 0
        print(f"{'STATE':12} {'STAGE':11} {'PRI':>4} {'%':>4}  {'JOB':36}  PATH")
        for job in jobs:
            item = session.get(MediaItem, job.media_item_id)
            print(
                f"{job.state:12} {(job.stage or ''):11} {job.priority:>4} "
                f"{job.progress_pct:>4.0f}  {job.id:36}  {item.path if item else ''}"
            )
    return 0


def _job_row(job: Any, item: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "state": job.state,
        "stage": job.stage,
        "trigger": job.trigger,
        "priority": job.priority,
        "attempts": job.attempts,
        "progress_pct": job.progress_pct,
        "error": job.error,
        "path": item.path if item else None,
        "work_dir": job.work_dir,
        "timings": json.loads(job.timings_json) if job.timings_json else {},
    }
