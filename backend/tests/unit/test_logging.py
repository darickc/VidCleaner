"""Logging configuration, and the stale-stream trap it has to avoid."""

from __future__ import annotations

import io
import sys

from vidcleaner.logging import configure_logging, get_logger


def test_logs_go_to_stderr_not_stdout(capsys):
    """`--json` output on stdout must stay parseable."""
    configure_logging("INFO", role="test")
    get_logger("t").info("hello.world", answer=42)
    captured = capsys.readouterr()
    assert "hello.world" in captured.err
    assert "hello.world" not in captured.out


def test_reconfiguring_does_not_capture_a_stale_stream(capsys):
    """The regression: a captured stream is closed once its scope ends.

    `PrintLoggerFactory(file=sys.stderr)` binds the stream object, and
    `cache_logger_on_first_use` keeps it forever -- so a logger configured while
    something had replaced `sys.stderr` would later raise
    `ValueError: I/O operation on closed file`.
    """
    replacement = io.StringIO()
    original = sys.stderr
    sys.stderr = replacement
    try:
        configure_logging("INFO", role="test")
        get_logger("t").info("during.replacement")
    finally:
        sys.stderr = original
    replacement.close()

    # Must not raise, and must reach the *current* stderr.
    get_logger("t").info("after.replacement")
    assert "after.replacement" in capsys.readouterr().err


def test_the_level_is_honoured(capsys):
    configure_logging("WARNING", role="test")
    log = get_logger("t")
    log.info("suppressed.event")
    log.warning("kept.event")
    captured = capsys.readouterr()
    assert "suppressed.event" not in captured.err
    assert "kept.event" in captured.err
    configure_logging("INFO", role="test")


def test_the_role_is_bound(capsys):
    configure_logging("INFO", role="worker", json=True)
    get_logger("t").info("event.name")
    assert '"role": "worker"' in capsys.readouterr().err
    configure_logging("INFO", role="test")
