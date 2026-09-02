"""``vidcleaner`` command line entry point.

``health``, ``words`` and ``backups`` are introspection; ``clean``, ``detect`` and
``restore`` do the work. PLAN.md §9 puts all of this behind a UI, which is M4 -- these
exist so each milestone's behaviour is demonstrable before the screens exist.
"""

from __future__ import annotations

import argparse
import json
import sys

from vidcleaner import __version__


def _health() -> int:
    from vidcleaner.api.health import _database, ffmpeg_info  # noqa: PLC0415
    from vidcleaner.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    payload = {
        "version": __version__,
        "role": settings.role,
        "config_dir": str(settings.config_dir),
        "database": _database(),
        "ffmpeg": ffmpeg_info(),
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["database"]["ok"] else 1


def _words(action: str, categories: str | None) -> int:
    from vidcleaner.db.constants import WORD_CATEGORIES  # noqa: PLC0415
    from vidcleaner.matching.compiler import (  # noqa: PLC0415
        DEFAULT_CATEGORIES,
        ProfileSpec,
        build_matcher,
    )
    from vidcleaner.matching.normalize import detect_censored  # noqa: PLC0415
    from vidcleaner.matching.wordlists import (  # noqa: PLC0415
        load_builtin_entries,
        load_never_match,
    )

    selected = (
        frozenset(c.strip() for c in categories.split(",") if c.strip())
        if categories
        else DEFAULT_CATEGORIES
    )
    unknown = selected - set(WORD_CATEGORIES)
    if unknown:
        print(f"unknown categories: {sorted(unknown)}", file=sys.stderr)
        return 64

    entries = load_builtin_entries()
    never = load_never_match()

    if action == "list":
        for entry in sorted(entries, key=lambda e: (e.category, e.canonical)):
            flag = " " if entry.enabled else "-"
            parent = f"  <- {entry.parent}" if entry.parent else ""
            print(
                f"{flag} {entry.category:10} {entry.canonical:22}"
                f" {len(entry.forms):3} forms{parent}"
            )
        return 0

    print(f"{'CATEGORY':12} {'ENTRIES':>8} {'ENABLED':>8} {'FORMS':>7}   in profile")
    for category in WORD_CATEGORIES:
        items = [e for e in entries if e.category == category]
        on = [e for e in items if e.enabled]
        forms = sum(len(e.forms) for e in on)
        mark = "yes" if category in selected else "no"
        print(f"{category:12} {len(items):8} {len(on):8} {forms:7}   {mark}")

    matcher = build_matcher(entries, ProfileSpec(categories=selected))
    active = len(matcher.entries)
    print(f"\nprofile   {sorted(selected)}")
    print(f"active    {active} entries, {sum(len(e.forms) for e in matcher.entries)} forms")
    print(f"hash      {matcher.profile_hash}")

    # The same gate as tests/unit/test_never_match.py, against the widest
    # possible matcher: every category, every entry force-enabled.
    from dataclasses import replace  # noqa: PLC0415

    widest = build_matcher(
        tuple(replace(e, enabled=True) for e in entries),
        ProfileSpec(categories=frozenset(WORD_CATEGORIES)),
    )
    failures = [(w, [m.canonical for m in widest.finditer(w)]) for w in never.regression_corpus]
    failures = [f for f in failures if f[1]]
    censored = [t for t in sorted(never.never_match) if detect_censored(t, never.never_match)]

    print(f"\ncorpus    {len(never.regression_corpus)} innocent words", end="  ")
    print("OK" if not failures else f"FAILED: {failures}")
    print(f"censored  {len(never.never_match)} never_match tokens", end="  ")
    print("OK" if not censored else f"FAILED: {censored}")

    if failures or censored:
        return 1
    return 0


def _restore(item: int | None, path: str | None) -> int:
    from vidcleaner.db.models import MediaItem  # noqa: PLC0415
    from vidcleaner.db.session import session_scope  # noqa: PLC0415
    from vidcleaner.logging import configure_logging  # noqa: PLC0415
    from vidcleaner.pipeline.persist import restore_item  # noqa: PLC0415

    configure_logging("WARNING", role="cli")
    with session_scope() as session:
        if item is None:
            from sqlalchemy import select  # noqa: PLC0415

            row = session.scalars(select(MediaItem).where(MediaItem.path == path)).first()
            if row is None:
                print(f"error: no tracked item at {path}", file=sys.stderr)
                return 66
            item = row.id
        try:
            report = restore_item(session, item)
        except (ValueError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    print(f"Restored   {report.restored_path}")
    if report.displaced_path:
        print(f"Cleaned    moved aside to {report.displaced_path}")
    if report.sidecars:
        print(f"Subtitles  {report.sidecars} sidecar(s) restored")
    for warning in report.warnings:
        print(f"           warning: {warning}")
    return 0


def _backups(action: str) -> int:
    from sqlalchemy import select  # noqa: PLC0415

    from vidcleaner.config import get_settings  # noqa: PLC0415
    from vidcleaner.db.models import Backup  # noqa: PLC0415
    from vidcleaner.db.session import session_scope  # noqa: PLC0415
    from vidcleaner.logging import configure_logging  # noqa: PLC0415
    from vidcleaner.pipeline.persist import reconcile_backups  # noqa: PLC0415
    from vidcleaner.settings_store import load_settings  # noqa: PLC0415

    configure_logging("WARNING", role="cli")
    settings = get_settings()

    if action == "reconcile":
        with session_scope() as session:
            report = reconcile_backups(
                session,
                settings.backups_dir,
                retention_days=load_settings(session).backup_retention_days,
            )
        print(f"adopted {report.adopted} untracked file(s), marked {report.purged} purged")
        return 0

    with session_scope() as session:
        rows = session.scalars(select(Backup).order_by(Backup.created_at.desc())).all()
        if not rows:
            print("no backups recorded")
            return 0
        print(f"{'ID':>5}  {'ITEM':>5}  {'STATE':10} {'SIZE':>10}  PATH")
        for row in rows:
            size = f"{(row.size or 0) / 2**30:.2f} GiB" if row.size else ""
            print(
                f"{row.id:>5}  {row.media_item_id:>5}  {row.state:10} {size:>10}  {row.backup_path}"
            )
    return 0


def _add_pipeline_args(parser: argparse.ArgumentParser, *, with_output: bool) -> None:
    from vidcleaner.cli_clean import add_arguments  # noqa: PLC0415

    add_arguments(parser, with_output=with_output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vidcleaner", description="VidCleaner utilities")
    parser.add_argument("--version", action="version", version=f"vidcleaner {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("health", help="print configuration and dependency status as JSON")

    clean = subparsers.add_parser("clean", help="mute profanity in a file and write a new MKV")
    _add_pipeline_args(clean, with_output=True)

    detect = subparsers.add_parser(
        "detect", help="find profanity and print the counts, without rendering"
    )
    _add_pipeline_args(detect, with_output=False)

    restore = subparsers.add_parser(
        "restore", help="put a cleaned file's original back and move the clean copy aside"
    )
    target = restore.add_mutually_exclusive_group(required=True)
    target.add_argument("--item", type=int, help="media item id (see `vidcleaner backups list`)")
    target.add_argument("--path", help="the library path of the cleaned file")

    backups = subparsers.add_parser("backups", help="inspect and reconcile kept originals")
    backups.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=("list", "reconcile"),
        help=(
            "list: every recorded backup; reconcile: adopt files in /backups with no row "
            "and mark rows whose file is gone"
        ),
    )

    words = subparsers.add_parser("words", help="inspect the built-in word lists")
    words.add_argument(
        "action",
        nargs="?",
        default="check",
        choices=("check", "list"),
        help="check: per-category stats plus the false-positive gate; list: every entry",
    )
    words.add_argument(
        "--categories",
        help="comma-separated categories to treat as the active profile"
        " (default: the shipped default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "health":
        return _health()
    if args.command == "words":
        return _words(args.action, args.categories)
    if args.command == "restore":
        return _restore(args.item, args.path)
    if args.command == "backups":
        return _backups(args.action)
    if args.command in {"clean", "detect"}:
        from vidcleaner.cli_clean import run_clean  # noqa: PLC0415

        return run_clean(args, detect_only=args.command == "detect")
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
