"""structlog configuration shared by the api and worker processes."""

from __future__ import annotations

import logging
import sys

import structlog


class _StderrLogger:
    """Writes to ``sys.stderr`` as it is *at call time*.

    ``structlog.PrintLoggerFactory(file=sys.stderr)`` captures the stream object
    when logging is configured and ``cache_logger_on_first_use`` then keeps it
    forever. Anything that replaces ``sys.stderr`` afterwards -- pytest's
    ``capsys``, or a second `configure_logging` call from the CLI -- leaves the
    cached logger writing to a closed file, which surfaces much later as
    ``ValueError: I/O operation on closed file``. Resolving the stream per call
    costs nothing and removes the whole failure mode.
    """

    __slots__ = ()

    def msg(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    log = debug = info = warn = warning = msg
    error = err = critical = fatal = exception = msg

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<_StderrLogger>"


class _StderrLoggerFactory:
    __slots__ = ()

    def __call__(self, *args: object) -> _StderrLogger:
        return _StderrLogger()


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
        logger_factory=_StderrLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(role=role)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
