"""Stage 1: inspect the source file (PLAN.md §6 step 1).

Pure parsing (``parse_probe``) is separated from I/O so it can be tested against
committed ffprobe JSON -- including shapes we cannot synthesize, such as DTS-HD
and bitmap subtitles.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from vidcleaner.pipeline import lang
from vidcleaner.pipeline.artifacts import (
    AudioStreamInfo,
    CodecPlan,
    ProbeResult,
    SubtitleStreamInfo,
    VideoStreamInfo,
)
from vidcleaner.pipeline.codecs import choose_clean_codec, from_stream
from vidcleaner.pipeline.stages import StaleSourceError
from vidcleaner.pipeline.workspace import Workspace

NAME = "probe"

__all__ = ["NAME", "fingerprint", "load", "parse_probe", "run", "wait_for_stable"]

#: PLAN.md §4: sha1(first 8 MB + last 8 MB + size).
FINGERPRINT_CHUNK = 8 * 1024 * 1024
#: §6 step 1's free-space guards.
WORK_HEADROOM = 1.3

_TAG_MARKER = "VIDCLEANER"
_TAG_PROFILE_HASH = "VIDCLEANER_PROFILE_HASH"


class ProbeError(RuntimeError):
    pass


# ------------------------------------------------------------------- parsing


def _tags(entry: dict[str, Any]) -> dict[str, str]:
    """ffprobe tag keys vary in case between muxers; normalise to lowercase."""
    return {str(k).lower(): str(v) for k, v in (entry.get("tags") or {}).items()}


def _dispositions(entry: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(k for k, v in (entry.get("disposition") or {}).items() if v))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _audio_bitrate(
    stream: dict[str, Any], tags: dict[str, str], duration: float | None
) -> int | None:
    """Resolve a bitrate, in the order real files actually provide one.

    ``stream.bit_rate`` is present for CBR bitstreams (AC-3, E-AC-3) but absent
    for AAC in Matroska. mkvmerge writes ``BPS``/``BPS-eng`` statistics tags on
    most library rips, and ``NUMBER_OF_BYTES`` lets us derive it otherwise.
    """
    if (direct := _as_int(stream.get("bit_rate"))) is not None:
        return direct
    for key, value in tags.items():
        is_bps = key == "bps" or key.startswith("bps-")
        if is_bps and (parsed := _as_int(value)) is not None:
            return parsed
    if duration and duration > 0:
        for key, value in tags.items():
            if key.startswith("number_of_bytes") and (parsed := _as_int(value)) is not None:
                return int(parsed * 8 / duration)
    return None


def _stream_duration(stream: dict[str, Any], tags: dict[str, str]) -> float | None:
    if (direct := _as_float(stream.get("duration"))) is not None:
        return direct
    raw = tags.get("duration")
    if raw and raw.count(":") == 2:
        try:
            hours, minutes, seconds = (float(p) for p in raw.split(":"))
            return hours * 3600 + minutes * 60 + seconds
        except ValueError:
            return None
    return None


def choose_source_audio(
    streams: list[AudioStreamInfo], preferred_language: str | None
) -> tuple[int, str]:
    """PLAN.md §6.1: default stream, else preferred language, else the first."""
    if not streams:
        raise ProbeError("the file has no audio streams")
    for stream in streams:
        if stream.is_default:
            return stream.typed_index, "default"
    for stream in streams:
        if lang.matches(stream.language, preferred_language):
            return stream.typed_index, "preferred_language"
    return streams[0].typed_index, "first"


def parse_probe(
    payload: dict[str, Any],
    *,
    path: Path,
    size: int,
    mtime: float,
    inode: int | None = None,
    fingerprint: str = "",
    preferred_language: str | None = "eng",
    lossless: bool = False,
    profile_hash: str = "",
) -> ProbeResult:
    """Turn ffprobe JSON into a :class:`ProbeResult`. Pure."""
    fmt = payload.get("format") or {}
    format_tags = _tags(fmt)
    duration = _as_float(fmt.get("duration"), 0.0) or 0.0

    video: list[VideoStreamInfo] = []
    audio: list[AudioStreamInfo] = []
    subtitles: list[SubtitleStreamInfo] = []
    attachments = 0

    for stream in payload.get("streams") or []:
        kind = stream.get("codec_type")
        tags = _tags(stream)
        dispositions = _dispositions(stream)

        if kind == "video":
            video.append(
                VideoStreamInfo(
                    index=_as_int(stream.get("index")) or 0,
                    typed_index=len(video),
                    codec_name=str(stream.get("codec_name") or ""),
                    width=_as_int(stream.get("width")),
                    height=_as_int(stream.get("height")),
                    pix_fmt=stream.get("pix_fmt"),
                    avg_frame_rate=stream.get("avg_frame_rate"),
                    duration=_stream_duration(stream, tags),
                    is_attached_pic="attached_pic" in dispositions,
                )
            )
        elif kind == "audio":
            stream_duration = _stream_duration(stream, tags)
            audio.append(
                AudioStreamInfo(
                    index=_as_int(stream.get("index")) or 0,
                    typed_index=len(audio),
                    codec_name=str(stream.get("codec_name") or ""),
                    profile=stream.get("profile"),
                    channels=_as_int(stream.get("channels")) or 2,
                    channel_layout=stream.get("channel_layout"),
                    sample_rate=_as_int(stream.get("sample_rate")),
                    bit_rate=_audio_bitrate(stream, tags, stream_duration or duration),
                    bits_per_raw_sample=_as_int(stream.get("bits_per_raw_sample")),
                    language=lang.normalize_tag(tags.get("language")),
                    title=tags.get("title"),
                    is_default="default" in dispositions,
                    is_forced="forced" in dispositions,
                    dispositions=dispositions,
                    start_time=_as_float(stream.get("start_time"), 0.0) or 0.0,
                    duration=stream_duration,
                )
            )
        elif kind == "subtitle":
            subtitles.append(
                SubtitleStreamInfo(
                    index=_as_int(stream.get("index")) or 0,
                    typed_index=len(subtitles),
                    codec_name=str(stream.get("codec_name") or ""),
                    language=lang.normalize_tag(tags.get("language")),
                    title=tags.get("title"),
                    is_default="default" in dispositions,
                    is_forced="forced" in dispositions,
                    dispositions=dispositions,
                )
            )
        elif kind == "attachment":
            attachments += 1

    typed_index, reason = choose_source_audio(audio, preferred_language)
    source = next(s for s in audio if s.typed_index == typed_index)
    plan: CodecPlan = choose_clean_codec(from_stream(source), lossless=lossless)

    # §4 idempotency: skip a file already cleaned for this exact profile.
    tagged_hash = format_tags.get(_TAG_PROFILE_HASH.lower(), "")
    already_clean = bool(profile_hash) and tagged_hash == profile_hash

    return ProbeResult(
        path=str(path),
        size=size,
        mtime=mtime,
        inode=inode,
        container_format=str(fmt.get("format_name") or ""),
        duration=duration,
        fingerprint=fingerprint,
        tags={k.upper(): v for k, v in format_tags.items() if k.startswith(_TAG_MARKER.lower())},
        video=video,
        audio=audio,
        subtitles=subtitles,
        chapter_count=len(payload.get("chapters") or []),
        attachment_count=attachments,
        source_audio_typed_index=typed_index,
        source_audio_reason=reason,  # type: ignore[arg-type]
        clean_codec=plan,
        already_clean=already_clean,
    )


# ------------------------------------------------------------------------ I/O


def fingerprint(path: Path, chunk: int = FINGERPRINT_CHUNK) -> str:
    """sha1(first 8 MB + last 8 MB + size), per PLAN.md §4.

    Cheap on a 40 GB file and stable across a rename, which is what makes it
    usable as ``media_items.source_fingerprint``.
    """
    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as handle:
        digest.update(handle.read(chunk))
        if size > chunk * 2:
            handle.seek(-chunk, 2)
            digest.update(handle.read(chunk))
    digest.update(str(size).encode("ascii"))
    return digest.hexdigest()


def wait_for_stable(
    path: Path, *, poll_s: float = 5.0, stable_polls: int = 2, cap_s: float = 300.0
) -> None:
    """Block until size and mtime stop changing (§6.1: an import may be in flight)."""
    import time  # noqa: PLC0415

    deadline = time.monotonic() + cap_s
    previous: tuple[int, float] | None = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
        except OSError as exc:
            # `StaleSourceError`, not `ProbeError`: `stages.py:StaleSourceError` has
            # always documented `probe` as a raise site, and it is the class that
            # reaches §6's re-resolve-and-requeue path in `policy.classify`. Raising
            # the generic error sent a vanished source down the ordinary retry
            # ladder, where re-reading the same missing path three times is all it
            # could ever do.
            raise StaleSourceError(NAME, f"source vanished: {path}") from exc
        current = (stat.st_size, stat.st_mtime)
        stable = stable + 1 if current == previous else 0
        if stable >= stable_polls:
            return
        previous = current
        time.sleep(poll_s)
    raise ProbeError(f"{path} never stopped changing within {cap_s:.0f}s")


def check_free_space(probe: ProbeResult, work_dir: Path, backups_dir: Path | None) -> list[str]:
    """§6.1: /work needs 1.3x the source, the backup volume 1.0x.

    An unreadable volume is reported as a **problem**, not swallowed. It used to be
    ignored, which meant an unmounted `/work` or `/backups` passed the guard silently
    and the job failed several stages later with something that looked nothing like
    "your volume is not mounted" -- the single most likely misconfiguration on a fresh
    install.
    """
    problems: list[str] = []
    needed_work = int(probe.size * WORK_HEADROOM)
    try:
        if shutil.disk_usage(work_dir).free < needed_work:
            problems.append(
                f"/work has less than {needed_work / 2**30:.1f} GiB free "
                f"(needs {WORK_HEADROOM:g}x the source)"
            )
    except OSError as exc:
        problems.append(f"cannot read free space on {work_dir} ({exc.strerror or exc})")
    if backups_dir is not None:
        try:
            if shutil.disk_usage(backups_dir).free < probe.size:
                problems.append(f"backups volume has less than {probe.size / 2**30:.1f} GiB free")
        except OSError as exc:
            problems.append(f"cannot read free space on {backups_dir} ({exc.strerror or exc})")
    return problems


def run(ctx) -> None:
    source = Path(ctx.spec.source_path)
    if not source.is_file():
        raise StaleSourceError(NAME, f"source is not a file: {source}")

    stat = source.stat()
    payload = ctx.runner.probe(source)
    result = parse_probe(
        payload,
        path=source,
        size=stat.st_size,
        mtime=stat.st_mtime,
        inode=stat.st_ino,
        fingerprint=fingerprint(source),
        preferred_language=ctx.settings.preferred_language,
        lossless=ctx.settings.clean_track_lossless,
        profile_hash=ctx.spec.profile_hash,
    )

    if not ctx.spec.dry_run:
        for problem in check_free_space(result, ctx.ws.root, ctx.deploy.backups_dir):
            ctx.log.warning("probe.low_disk", problem=problem)

    ctx.log.info(
        "probe.done",
        duration=round(result.duration, 2),
        audio=len(result.audio),
        subtitles=len(result.subtitles),
        source_audio=f"a:{result.source_audio_typed_index}",
        reason=result.source_audio_reason,
        codec=result.clean_codec.encoder,
        bitrate=result.clean_codec.bit_rate,
        codec_reason=result.clean_codec.reason,
        already_clean=result.already_clean,
    )
    result.write(ctx.ws.probe_json)


def load(ws: Workspace) -> ProbeResult:
    return ProbeResult.read(ws.probe_json)
