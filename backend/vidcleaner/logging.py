"""structlog configuration shared by the api and worker processes."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", role: str = "all", *, json: bool | None = None) -> None:
    """Console rendering on a TTY (dev), JSON otherwise (container logs)."""
    if json is None:
        json = not sys.stderr.isatty()

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())

    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(role=role)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
