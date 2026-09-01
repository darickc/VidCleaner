"""Worker entry point: ``python -m vidcleaner.worker_main``."""

from __future__ import annotations

import signal
import sys

from vidcleaner.config import get_settings
from vidcleaner.db.migrate import upgrade_to_head
from vidcleaner.logging import configure_logging
from vidcleaner.worker.runner import Worker


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, role="worker")
    settings.ensure_dirs()
    if settings.auto_migrate:
        upgrade_to_head(settings)

    worker = Worker(settings)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.request_stop())

    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
