"""``vidcleaner`` command line entry point.

M0 exposes only introspection. The pipeline commands from PLAN.md §11 --
``vidcleaner clean <file> [--dry-run|--out]`` and ``vidcleaner detect <file>`` -- land
with M1, alongside the pipeline stages they drive.
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vidcleaner", description="VidCleaner utilities")
    parser.add_argument("--version", action="version", version=f"vidcleaner {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("health", help="print configuration and dependency status as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "health":
        return _health()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
