"""The only place ffmpeg and ffprobe are invoked (CLAUDE.md convention).

Empirically verified against the ffmpeg actually installed, because three of
PLAN.md §3's stated behaviours are wrong on ffmpeg 9 (all logged in §14):

* **``-filter_complex_script`` no longer exists** (``Unrecognized option`` on
  9.0.1). The replacement is the generic read-option-from-file syntax
  ``-/filter_complex <file>``, added in 7.0, which works on both 7.x and 9.x.
  :func:`get_caps` version-gates it so a pre-7.0 host still works.
* **``volumedetect`` prints its stats twice** -- once at graph-configuration
  time with ``n_samples: 0``, then for real. :func:`parse_volumedetect` takes the
  last block with a non-zero sample count.
* ``-af`` cannot be combined with a ``-filter_complex`` output label, so
  measurement filters go *inside* the graph when one is in use and via ``-af``
  only when it is not.

``api/health.py::ffmpeg_info()`` stays as it is: that is a version probe, not
pipeline work.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import TimeRange
from vidcleaner.pipeline.workspace import atomic_write_text

__all__ = [
    "FFmpegError",
    "FFmpegMissing",
    "FFmpegProgress",
    "FFmpegResult",
    "FFmpegRunner",
    "FfmpegCaps",
    "SilenceSpan",
    "VolumeStats",
    "get_caps",
    "parse_silencedetect",
    "parse_volumedetect",
    "write_filter_script",
]

log = get_logger(__name__)

#: The floor `volume=0` produces, measured: exactly -91.0 dB for s16.
SILENT_DB = -91.0
#: Assertion threshold. -91 is the s16 quantization floor and shifts under FLAC
#: (s32) and lossy ringing, so "inaudible" is asserted at -80.
INAUDIBLE_DB = -80.0

MIN_VERSION = (7, 0)
_STDERR_TAIL = 200

_VERSION_RE = re.compile(r"version\s+n?(\d+)\.(\d+)")
_VOLUME_RE = re.compile(
    r"n_samples:\s*(\d+)|mean_volume:\s*(-?[\d.]+) dB|max_volume:\s*(-?[\d.]+) dB"
)
_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


class FFmpegMissing(RuntimeError):
    """ffmpeg or ffprobe is not on PATH."""


class FFmpegError(RuntimeError):
    def __init__(
        self,
        label: str,
        args: Sequence[str],
        returncode: int,
        stderr_tail: str,
        *,
        log_path: Path | None = None,
        timed_out: bool = False,
    ) -> None:
        last = next((ln for ln in reversed(stderr_tail.splitlines()) if ln.strip()), "no stderr")
        suffix = f"; full log: {log_path}" if log_path else ""
        reason = "timed out" if timed_out else f"failed (rc={returncode})"
        super().__init__(f"ffmpeg {label} {reason}: {last}{suffix}")
        self.label = label
        # NOT `self.args`: that is a BaseException slot, and assigning it
        # replaces the tuple `str(exc)` is derived from, losing the message.
        self.argv = list(args)
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        self.log_path = log_path
        self.timed_out = timed_out


@dataclass(frozen=True, slots=True)
class FFmpegResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr_tail: str
    elapsed_s: float
    log_path: Path | None = None


@dataclass(frozen=True, slots=True)
class FFmpegProgress:
    out_time_s: float
    speed: float | None = None
    total_size: int | None = None
    fraction: float | None = None


@dataclass(frozen=True, slots=True)
class VolumeStats:
    mean_db: float
    max_db: float
    n_samples: int

    @property
    def inaudible(self) -> bool:
        return self.max_db <= INAUDIBLE_DB


@dataclass(frozen=True, slots=True)
class SilenceSpan:
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class FfmpegCaps:
    ffmpeg: str
    ffprobe: str
    version: tuple[int, int]
    filter_script_flag: str
    encoders: frozenset[str] = field(default_factory=frozenset)

    @property
    def version_str(self) -> str:
        return f"{self.version[0]}.{self.version[1]}"

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders


def _which(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise FFmpegMissing(f"{name} is not on PATH")
    return found


@lru_cache(maxsize=1)
def get_caps() -> FfmpegCaps:
    """Probe the installed ffmpeg once. Raises :class:`FFmpegMissing` if absent."""
    ffmpeg = _which("ffmpeg")
    ffprobe = _which("ffprobe")

    first = subprocess.run(
        [ffmpeg, "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    ).stdout.splitlines()
    match = _VERSION_RE.search(first[0] if first else "")
    version = (int(match.group(1)), int(match.group(2))) if match else (0, 0)

    # Verified: 9.0.1 rejects -filter_complex_script outright; -/filter_complex
    # landed in 7.0 and works on both. The Dockerfile already refuses < 7.0,
    # so the legacy spelling is dev-machine insurance only.
    flag = "-/filter_complex" if version >= MIN_VERSION else "-filter_complex_script"

    listing = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout
    encoders = {
        parts[1]
        for line in listing.splitlines()
        if (parts := line.split()) and len(parts) >= 2 and parts[0][:1] in "AVSD."
    }

    caps = FfmpegCaps(ffmpeg, ffprobe, version, flag, frozenset(encoders))
    log.info(
        "ffmpeg.caps",
        version=caps.version_str,
        filter_script_flag=flag,
        encoders=len(encoders),
    )
    return caps


def write_filter_script(path: Path, graph: str) -> Path:
    """Persist a filter graph. The only way one ever reaches ffmpeg.

    PLAN.md §3: the Linux single-argument cap is 128 KB and a feature film's
    graph exceeds it (measured: 143 KB at ~1600 mute ranges), so this is not an
    optimisation.
    """
    atomic_write_text(path, graph if graph.endswith("\n") else graph + "\n")
    return path


def parse_volumedetect(stderr: str) -> VolumeStats | None:
    """Take the LAST stats block, ignoring the ``n_samples: 0`` config-time one."""
    blocks: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for line in stderr.splitlines():
        if "n_samples:" in line:
            if current:
                blocks.append(current)
            current = {}
        for match in _VOLUME_RE.finditer(line):
            samples, mean, peak = match.groups()
            if samples is not None:
                current["n_samples"] = float(samples)
            elif mean is not None:
                current["mean"] = float(mean)
            elif peak is not None:
                current["max"] = float(peak)
    if current:
        blocks.append(current)

    for block in reversed(blocks):
        if block.get("n_samples", 0) > 0 and "mean" in block and "max" in block:
            return VolumeStats(block["mean"], block["max"], int(block["n_samples"]))
    return None


def parse_silencedetect(stderr: str) -> list[SilenceSpan]:
    starts: list[float] = []
    spans: list[SilenceSpan] = []
    for line in stderr.splitlines():
        if (m := _SILENCE_START_RE.search(line)) is not None:
            starts.append(float(m.group(1)))
        if (m := _SILENCE_END_RE.search(line)) is not None:
            end = float(m.group(1))
            spans.append(SilenceSpan(starts.pop() if starts else 0.0, end))
    return spans


def _parse_progress(block: dict[str, str], total: float | None) -> FFmpegProgress:
    # `out_time_ms` is a long-standing misnomer -- it reports MICROseconds --
    # so prefer out_time_us, then out_time_ms, then the formatted out_time.
    seconds = 0.0
    if (raw := block.get("out_time_us") or block.get("out_time_ms")) is not None:
        try:
            seconds = int(raw) / 1_000_000.0
        except ValueError:
            seconds = 0.0
    elif (stamp := block.get("out_time")) is not None:
        parts = stamp.split(":")
        try:
            hours, minutes, secs = (float(p) for p in parts)
            seconds = hours * 3600 + minutes * 60 + secs
        except ValueError:
            seconds = 0.0

    speed = None
    if (raw_speed := block.get("speed")) is not None:
        try:
            speed = float(raw_speed.rstrip("x").strip())
        except ValueError:
            speed = None

    size = None
    if (raw_size := block.get("total_size")) is not None:
        try:
            size = int(raw_size)
        except ValueError:
            size = None

    fraction = None
    if total and total > 0:
        fraction = max(0.0, min(1.0, seconds / total))
    return FFmpegProgress(seconds, speed, size, fraction)


class FFmpegRunner:
    """Runs ffmpeg/ffprobe and owns the per-job stderr log. One per job."""

    def __init__(self, log_path: Path | None = None, *, default_timeout: float = 3600.0) -> None:
        self.log_path = log_path
        self.default_timeout = default_timeout

    # ---------------------------------------------------------------- ffprobe

    def probe(self, path: Path, *, chapters: bool = True, timeout: float = 120.0) -> dict[str, Any]:
        import json  # noqa: PLC0415

        caps = get_caps()
        args = [
            caps.ffprobe,
            "-v",
            "error",
            "-hide_banner",
            "-show_format",
            "-show_streams",
        ]
        if chapters:
            args.append("-show_chapters")
        args += ["-of", "json", str(path)]

        started = time.monotonic()
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        self._append_log("ffprobe", args, completed.stderr)
        if completed.returncode != 0:
            raise FFmpegError(
                "ffprobe", args, completed.returncode, completed.stderr, log_path=self.log_path
            )
        log.debug("ffprobe.done", path=str(path), elapsed_s=round(time.monotonic() - started, 3))
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FFmpegError("ffprobe", args, 0, f"unparseable JSON: {exc}") from exc

    # ----------------------------------------------------------------- ffmpeg

    def run(
        self,
        args: Sequence[str],
        *,
        label: str,
        timeout: float | None = None,
        on_progress: Callable[[FFmpegProgress], None] | None = None,
        total_duration: float | None = None,
        loglevel: str = "error",
        check: bool = True,
    ) -> FFmpegResult:
        """Run ffmpeg with ``args`` (everything after the binary name)."""
        caps = get_caps()
        # -nostdin is mandatory: the worker is a daemon and ffmpeg would
        # otherwise consume its stdin.
        argv = [caps.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", loglevel]
        if on_progress is not None:
            argv += ["-progress", "pipe:1", "-nostats"]
        argv += list(args)

        self._append_log(label, argv, "")
        log.debug("ffmpeg.start", label=label, argv=argv)

        started = time.monotonic()
        process = subprocess.Popen(  # noqa: S603
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        tail: deque[str] = deque(maxlen=_STDERR_TAIL)
        drain = threading.Thread(target=self._drain_stderr, args=(process, tail), daemon=True)
        drain.start()

        # stdout is drained on its own thread so that `process.wait(timeout=...)`
        # below is the authoritative deadline. Reading it inline would block
        # until EOF and make the timeout unreachable.
        stdout_parts: list[str] = []
        pump = threading.Thread(
            target=self._drain_stdout,
            args=(process, stdout_parts, on_progress, total_duration, label),
            daemon=True,
        )
        pump.start()

        timed_out = False
        try:
            process.wait(timeout=timeout or self.default_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        drain.join(timeout=5)
        pump.join(timeout=5)

        elapsed = time.monotonic() - started
        stderr_tail = "".join(tail)
        result = FFmpegResult(
            argv,
            process.returncode or 0,
            "".join(stdout_parts),
            stderr_tail,
            elapsed,
            self.log_path,
        )

        if timed_out or (check and result.returncode != 0):
            log.error(
                "ffmpeg.failed",
                label=label,
                rc=result.returncode,
                timed_out=timed_out,
                stderr_tail=stderr_tail[-2000:],
            )
            raise FFmpegError(
                label,
                argv,
                result.returncode,
                stderr_tail,
                log_path=self.log_path,
                timed_out=timed_out,
            )
        log.info("ffmpeg.done", label=label, rc=result.returncode, elapsed_s=round(elapsed, 3))
        return result

    def run_filtered(
        self,
        *,
        input_args: Sequence[str],
        graph: str,
        graph_path: Path,
        output_args: Sequence[str],
        label: str,
        on_progress: Callable[[FFmpegProgress], None] | None = None,
        total_duration: float | None = None,
        loglevel: str = "error",
    ) -> FFmpegResult:
        """Write ``graph`` to disk and splice in the filter-script flag."""
        write_filter_script(graph_path, graph)
        caps = get_caps()
        args = [
            *input_args,
            caps.filter_script_flag,
            str(graph_path),
            *output_args,
        ]
        return self.run(
            args,
            label=label,
            on_progress=on_progress,
            total_duration=total_duration,
            loglevel=loglevel,
        )

    # ------------------------------------------------------------ measurement

    def measure_volume(
        self,
        path: Path,
        *,
        stream: str = "0:a:0",
        window: TimeRange | None = None,
        label: str = "volumedetect",
    ) -> VolumeStats | None:
        """Measure one window's level.

        Input-side ``-ss``/``-t`` keeps this cheap on a two-hour file. No
        ``-filter_complex`` is in play, so ``-af`` is safe here.
        """
        args: list[str] = []
        if window is not None:
            args += ["-ss", f"{window.start:.3f}", "-t", f"{max(0.001, window.duration):.3f}"]
        args += ["-i", str(path), "-map", stream, "-af", "volumedetect", "-f", "null", "-"]
        result = self.run(args, label=label, loglevel="info", timeout=300)
        return parse_volumedetect(result.stderr_tail)

    def detect_silence(
        self,
        path: Path,
        *,
        stream: str = "0:a:0",
        threshold_db: float = -60.0,
        min_duration: float = 0.02,
        label: str = "silencedetect",
    ) -> list[SilenceSpan]:
        """Silence spans across the whole stream, for boundary assertions."""
        args = [
            "-i",
            str(path),
            "-map",
            stream,
            "-af",
            f"silencedetect=n={threshold_db}dB:d={min_duration}",
            "-f",
            "null",
            "-",
        ]
        result = self.run(args, label=label, loglevel="info", timeout=1800)
        return parse_silencedetect(result.stderr_tail)

    def decode_check(self, path: Path, *, stream: str = "0:a:0") -> str:
        """Full decode of one stream. Returns stderr, which must be empty."""
        result = self.run(
            ["-i", str(path), "-map", stream, "-f", "null", "-"],
            label="decode_check",
            loglevel="error",
            timeout=3600,
        )
        return result.stderr_tail.strip()

    # -------------------------------------------------------------------- log

    def _drain_stdout(
        self,
        process: subprocess.Popen[str],
        sink: list[str],
        on_progress: Callable[[FFmpegProgress], None] | None,
        total_duration: float | None,
        label: str,
    ) -> None:
        """Consume stdout, parsing ``-progress`` key=value blocks as they arrive."""
        if process.stdout is None:
            return
        block: dict[str, str] = {}
        try:
            for line in process.stdout:
                sink.append(line)
                if on_progress is None:
                    continue
                key, sep, value = line.strip().partition("=")
                if not sep:
                    continue
                if key == "progress":
                    try:
                        on_progress(_parse_progress(block, total_duration))
                    except Exception:  # pragma: no cover - reporters never fail a job
                        log.warning("ffmpeg.progress_callback_failed", label=label)
                    block = {}
                else:
                    block[key] = value
        except (OSError, ValueError):  # pragma: no cover - pipe torn down on kill
            pass

    def _drain_stderr(self, process: subprocess.Popen[str], tail: deque[str]) -> None:
        """Stream stderr to the job log and keep only a bounded tail in memory.

        Never ``capture_output=True`` on a render: a 4 GB movie's warning spam
        would sit in RAM.
        """
        if process.stderr is None:
            return
        handle = None
        try:
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.log_path.open("a", encoding="utf-8")
            for line in process.stderr:
                tail.append(line)
                if handle is not None:
                    handle.write(line)
        except OSError:  # pragma: no cover - a broken log must not fail the job
            pass
        finally:
            if handle is not None:
                handle.close()

    def _append_log(self, label: str, args: Sequence[str], stderr: str) -> None:
        """Record the invocation so the job log reads as a replayable script."""
        if self.log_path is None:
            return
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n==== {label} {stamp} ====\n$ {shlex.join(args)}\n")
                if stderr:
                    handle.write(stderr if stderr.endswith("\n") else stderr + "\n")
        except OSError:  # pragma: no cover
            pass
