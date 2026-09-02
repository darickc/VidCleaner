"""``vidcleaner`` command line entry point.

``health`` and ``words`` are introspection. The pipeline commands from PLAN.md §11 --
``vidcleaner clean <file> [--dry-run|--out]`` and ``vidcleaner detect <file>`` -- arrive
with the render stages they drive.
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vidcleaner", description="VidCleaner utilities")
    parser.add_argument("--version", action="version", version=f"vidcleaner {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("health", help="print configuration and dependency status as JSON")

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
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
