"""Putting the cleaned file into the library -- PLAN.md §6 step 8.

This is the only module that changes library files (CLAUDE.md), the only stage that
is not reversible by deleting an artifact, and therefore the only one whose failure
modes all have to be enumerated rather than trusted to a retry.

**Rename-only.** The original is *moved* to ``/backups``, never copied-and-deleted:
the swap has to be reversible and a delete is not. The one file this module ever
unlinks is its own staging temp.

**The order is load-bearing.** ``rename(original -> backup)`` happens *first*, so
there is never a moment when the library folder holds two video files -- §3 warns
that a stray sibling can be adopted by Sonarr as *the* file, which is a data-loss
shaped outcome. The cost is a window, one rename wide, where the folder holds none;
that is much cheaper, because neither arr deletes anything during a scan and the
episode merely reads as missing until the next moment.

**Two renames cannot be made atomic**, so before the first one this module writes an
fsynced intent journal (``swap.plan.json``). §6's "on failure rename(backup ->
original)" only covers an *exception*; a power loss leaves a directory with no video
file and, without the journal, nothing on disk that says why. :func:`recover` reads
the journal and resolves every reachable state.

**Every OS call goes through :class:`FsOps`.** Not for purity's sake -- it is the only
way to test EXDEV, ENOSPC, EACCES and each individual crash point deterministically,
and those are exactly the paths that must work the first time they happen for real.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from vidcleaner.logging import get_logger
from vidcleaner.pipeline.artifacts import (
    ProbeResult,
    RenderResult,
    SidecarSwap,
    SwapPlan,
    SwapResult,
    VerifyResult,
)
from vidcleaner.pipeline.probe import fingerprint
from vidcleaner.pipeline.stages import StageError, StaleSourceError, SwapBrokenError
from vidcleaner.pipeline.workspace import Workspace

__all__ = [
    "NAME",
    "STAGED_PREFIX",
    "STAGED_SUFFIX",
    "FsOps",
    "RealFs",
    "RestorePlan",
    "RestoreResult",
    "Recovery",
    "backup_path_for",
    "execute",
    "load",
    "plan_swap",
    "preflight",
    "recover",
    "reconcile",
    "restore_backup",
    "run",
    "write_ignore_marker",
]

NAME = "swap"
log = get_logger(__name__)

#: The staged file must not end in a video extension: §3's "never leave a second
#: video file in the folder" is about what an arr's disk scan enumerates, and it
#: enumerates by extension. The leading dot is belt and braces.
STAGED_PREFIX = ".vidcleaner."
STAGED_SUFFIX = ".tmp"
#: Headroom demanded on the library filesystem when the output has to be copied
#: there rather than renamed.
STAGE_HEADROOM_BYTES = 64 * 1024 * 1024
#: Written into ``/backups`` on first use. `/backups` defaults to a directory inside
#: the media share (§10), and Jellyfin honours this file.
IGNORE_MARKER = ".ignore"


# --------------------------------------------------------------- filesystem


class FsOps(Protocol):
    """Every filesystem operation the swap performs, so all of them are testable."""

    def stat(self, path: Path) -> os.stat_result: ...
    def exists(self, path: Path) -> bool: ...
    def rename(self, src: Path, dst: Path) -> None: ...
    def copy_file(self, src: Path, dst: Path) -> None: ...
    def unlink(self, path: Path) -> None: ...
    def mkdirs(self, path: Path) -> None: ...
    def chmod(self, path: Path, mode: int) -> None: ...
    def chown(self, path: Path, uid: int, gid: int) -> None: ...
    def disk_free(self, path: Path) -> int: ...
    def same_device(self, a: Path, b: Path) -> bool: ...
    def write_text(self, path: Path, text: str) -> None: ...


class RealFs:
    """The default :class:`FsOps`."""

    def stat(self, path: Path) -> os.stat_result:
        return path.stat()

    def exists(self, path: Path) -> bool:
        return path.exists()

    def rename(self, src: Path, dst: Path) -> None:
        os.rename(src, dst)

    def copy_file(self, src: Path, dst: Path) -> None:
        """Streamed, then fsynced -- both the file and its directory.

        Without the fsync the copy can be in page cache only, and this runs
        immediately before renames that we are about to promise are durable.
        """
        with open(src, "rb") as reader, open(dst, "wb") as writer:
            shutil.copyfileobj(reader, writer, length=8 * 1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        directory = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def unlink(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def mkdirs(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def chmod(self, path: Path, mode: int) -> None:
        os.chmod(path, mode)

    def chown(self, path: Path, uid: int, gid: int) -> None:
        os.chown(path, uid, gid)

    def disk_free(self, path: Path) -> int:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return 0

    def same_device(self, a: Path, b: Path) -> bool:
        """Only ever used to *plan*, never to decide.

        unraid's ``/mnt/user`` is a FUSE shfs mount: two paths in one share report
        the same ``st_dev`` while the underlying disks differ. So the real call is
        always try-``rename``-then-fall-back-on-``EXDEV``, and this only chooses
        which to attempt first.
        """
        try:
            return (
                self.stat(_existing_ancestor(a)).st_dev == self.stat(_existing_ancestor(b)).st_dev
            )
        except OSError:
            return False

    def write_text(self, path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path  # pragma: no cover - the filesystem root always exists


def _is_exdev(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV


def _cross_device_message(source: Path, backup: Path) -> str:
    """The one actionable wording for "/backups is on another filesystem".

    Shared by :func:`preflight` and by the execute-time EXDEV fallback, because
    ``same_device`` only *plans*: on unraid's FUSE shfs -- and, as the M5 demo found,
    inside Docker -- two separate mounts can report the same ``st_dev``, so the split
    is discovered by the rename failing rather than by the plan. Both paths must say
    the same thing, or the most likely misconfiguration on a fresh install surfaces as
    a bare ``[Errno 18] Invalid cross-device link``.
    """
    return (
        f"the backup directory {backup.parent} is on a different filesystem than "
        f"{source.parent}, so the original cannot be moved there without deleting it. "
        "Point /backups at the same share as the library, or set "
        "allow_cross_device_backup."
    )


# --------------------------------------------------------------- planning


def backup_path_for(
    source: Path, *, media_dir: Path, backups_dir: Path, job_id: str, fs: FsOps
) -> Path:
    """Mirror the library layout under ``/backups``.

    Browsable, and it makes "which episode is this?" answerable without the
    database. The extension is kept real because §6's audit pass re-renders from the
    backup and has to be able to probe it.
    """
    try:
        relative = source.resolve().relative_to(media_dir.resolve())
    except (ValueError, OSError):
        # Outside /media (a CLI run on a file anywhere). Keep the shape without
        # letting an absolute path escape the backups directory.
        relative = Path("_abs") / Path(*source.resolve().parts[1:])
    candidate = backups_dir / relative
    if not fs.exists(candidate):
        return candidate
    # A second clean of the same episode (after a restore, or an upgrade). Do not
    # overwrite the earlier original; the job id makes it traceable.
    return candidate.with_name(f"{candidate.stem}.vc-{job_id[:8]}{candidate.suffix}")


def _staged_path(final: Path) -> Path:
    return final.parent / f"{STAGED_PREFIX}{final.name}{STAGED_SUFFIX}"


def plan_swap(
    spec: Any,
    probe: ProbeResult,
    render: RenderResult,
    *,
    media_dir: Path,
    backups_dir: Path,
    fs: FsOps | None = None,
) -> SwapPlan:
    """Everything the transaction will do, decided before it does any of it."""
    fs = fs or RealFs()
    source = Path(probe.path)
    out = Path(render.out_path)

    # MP4 in, MKV out (§2). The old name is renamed to the backup, so no sibling
    # video file is left for an arr to adopt.
    final = source if source.suffix.lower() == ".mkv" else source.with_suffix(".mkv")
    backup = backup_path_for(
        source, media_dir=media_dir, backups_dir=backups_dir, job_id=spec.job_id, fs=fs
    )
    staged = _staged_path(final)

    try:
        stat = fs.stat(source)
    except OSError as exc:
        # Raised here, not left as a bare OSError: `preflight` classifies a vanished
        # source as `stale` (§6's "path vanished" is a recovery path), but planning
        # runs first and would otherwise make it a terminal swap failure.
        raise StaleSourceError(NAME, f"source is gone: {source}") from exc
    return SwapPlan(
        job_id=spec.job_id,
        source_path=str(source),
        final_path=str(final),
        backup_path=str(backup),
        staged_path=str(staged),
        source_size=stat.st_size,
        source_inode=getattr(stat, "st_ino", None),
        out_size=render.size or (fs.stat(out).st_size if fs.exists(out) else 0),
        extension_changed=final != source,
        stage_via="rename" if fs.same_device(out, final.parent) else "copy",
        backup_via="rename" if fs.same_device(source, backup.parent) else "copy",
        mode=stat.st_mode & 0o7777,
        uid=getattr(stat, "st_uid", None),
        gid=getattr(stat, "st_gid", None),
        sidecars=_plan_sidecars(render, media_dir=media_dir, backups_dir=backups_dir, fs=fs),
        created_at=datetime.now(UTC),
    )


def _plan_sidecars(
    render: RenderResult, *, media_dir: Path, backups_dir: Path, fs: FsOps
) -> list[SidecarSwap]:
    out: list[SidecarSwap] = []
    for entry in render.redacted:
        if not entry.sidecar_source:
            continue
        original = Path(entry.sidecar_source)
        out.append(
            SidecarSwap(
                original_path=str(original),
                backup_path=str(
                    backup_path_for(
                        original,
                        media_dir=media_dir,
                        backups_dir=backups_dir,
                        job_id=render.tags.get("VIDCLEANER_JOB", "job"),
                        fs=fs,
                    )
                ),
                staged_path=entry.output_path,
                replacements=entry.replacements,
            )
        )
    return out


# --------------------------------------------------------------- preflight


def preflight(
    plan: SwapPlan,
    *,
    allow_cross_device_backup: bool = False,
    fs: FsOps | None = None,
    arr_paths: tuple[str, ...] = (),
) -> None:
    """Refuse before touching anything. Every raise here leaves the library intact."""
    fs = fs or RealFs()
    source = Path(plan.source_path)
    final = Path(plan.final_path)
    backup = Path(plan.backup_path)

    # The source must be the file we probed and rendered from. `verify` checks this
    # too, but minutes of encoding separate the two and the check that matters is
    # the one immediately before the first rename.
    try:
        stat = fs.stat(source)
    except OSError as exc:
        raise StaleSourceError(NAME, f"source is gone: {source}") from exc
    if stat.st_size != plan.source_size:
        raise StaleSourceError(
            NAME, f"source changed size since probe ({stat.st_size} != {plan.source_size})"
        )
    if plan.source_inode is not None and getattr(stat, "st_ino", None) != plan.source_inode:
        raise StaleSourceError(NAME, "source was replaced since probe (inode changed)")

    if plan.backup_via == "copy" and not allow_cross_device_backup:
        raise StageError(NAME, _cross_device_message(source, backup))

    if fs.exists(backup):
        raise StageError(NAME, f"backup path already exists: {backup}")

    # An MP4 input becomes an MKV; if something else already owns that name we know
    # nothing about it and must not clobber it.
    if plan.extension_changed and fs.exists(final):
        raise StageError(NAME, f"{final} already exists; refusing to overwrite it")

    if plan.stage_via == "copy":
        free = fs.disk_free(final.parent)
        needed = plan.out_size + STAGE_HEADROOM_BYTES
        if free < needed:
            raise StageError(
                NAME,
                f"{final.parent} has {free / 2**30:.1f} GiB free; staging the cleaned "
                f"file there needs {needed / 2**30:.1f} GiB",
            )

    for arr_path in arr_paths:
        if arr_path and _is_within(backup.parent, Path(arr_path)):
            raise StageError(
                NAME,
                f"the backup directory {backup.parent} is inside the library folder "
                f"{arr_path}; Sonarr/Radarr would see two video files for one episode",
            )


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
    except (ValueError, OSError):
        return False
    return True


# --------------------------------------------------------------- execution


@dataclass(frozen=True, slots=True)
class Recovery:
    action: Literal["none", "redo", "committed", "rolled_back", "broken", "stale"]
    detail: str = ""
    result: SwapResult | None = None


def _sized(fs: FsOps, path: Path, size: int) -> bool:
    try:
        return fs.stat(path).st_size == size
    except OSError:
        return False


def recover(plan: SwapPlan, *, fs: FsOps | None = None) -> Recovery:
    """Resolve whatever state a crashed swap left behind.

    Decided by **size**, not mere existence, because that is the only question that
    distinguishes an MKV swapped in place (where source and final are one path) from
    an MP4 that became an MKV, and it also catches a short copy.
    """
    fs = fs or RealFs()
    source = Path(plan.source_path)
    final = Path(plan.final_path)
    backup = Path(plan.backup_path)
    staged = Path(plan.staged_path)

    if _sized(fs, final, plan.out_size) and plan.out_size:
        # Committed. Also the answer for a *forced* re-run of a finished swap, which
        # would otherwise try to back up our own cleaned output.
        detail = "committed"
        warnings = []
        if not fs.exists(backup):
            warnings.append(f"backup is missing from {backup}")
            detail = "committed_without_backup"
        return Recovery(
            "committed",
            detail,
            SwapResult(
                original_path=plan.source_path,
                final_path=plan.final_path,
                backup_path=plan.backup_path,
                old_path=plan.source_path if plan.extension_changed else None,
                backup_size=plan.source_size,
                out_size=plan.out_size,
                stage_via=plan.stage_via,
                backup_via=plan.backup_via,
                recovered_from=detail,
                warnings=warnings,
            ),
        )

    backup_done = _sized(fs, backup, plan.source_size)

    if backup_done and fs.exists(source):
        # Both the original and its backup exist: rename #1 behaved as a copy. We
        # cannot tell which is authoritative, so stop rather than guess.
        return Recovery(
            "broken",
            f"both {source} and {backup} exist; the library is in an unexpected state",
        )

    if backup_done:
        if _sized(fs, staged, plan.out_size) and plan.out_size:
            # Roll *forward*: those bytes already passed verification. Rolling back
            # would throw away a good render for no reason.
            try:
                fs.rename(staged, final)
            except OSError as exc:
                return Recovery("broken", f"could not install the staged file: {exc}")
            return Recovery(
                "committed",
                "rolled_forward",
                SwapResult(
                    original_path=plan.source_path,
                    final_path=plan.final_path,
                    backup_path=plan.backup_path,
                    old_path=plan.source_path if plan.extension_changed else None,
                    backup_size=plan.source_size,
                    out_size=plan.out_size,
                    stage_via=plan.stage_via,
                    backup_via=plan.backup_via,
                    recovered_from="rolled_forward",
                ),
            )
        # Nothing usable staged: put the original back and re-render.
        try:
            fs.rename(backup, source)
        except OSError as exc:
            return Recovery("broken", f"could not restore {source} from {backup}: {exc}")
        fs.unlink(staged)
        return Recovery("rolled_back", "no usable staged output; the original is back in place")

    if _sized(fs, source, plan.source_size):
        fs.unlink(staged)
        return Recovery("redo", "nothing was committed")

    return Recovery("stale", f"neither {source} nor {backup} holds the original")


def execute(
    plan: SwapPlan,
    *,
    out_path: Path,
    fs: FsOps | None = None,
    allow_cross_device_backup: bool = False,
) -> SwapResult:
    """Stage, back up, install. Raises before mutating anything it can."""
    import time  # noqa: PLC0415

    fs = fs or RealFs()
    started = time.monotonic()
    source = Path(plan.source_path)
    final = Path(plan.final_path)
    backup = Path(plan.backup_path)
    staged = Path(plan.staged_path)
    warnings: list[str] = []

    # --- stage the output next to its final home
    staged_by_rename = False
    fs.mkdirs(final.parent)
    if plan.stage_via == "rename":
        try:
            fs.rename(out_path, staged)
            staged_by_rename = True
        except OSError as exc:
            if not _is_exdev(exc):
                raise StageError(NAME, f"could not stage {out_path}: {exc}") from exc
            fs.copy_file(out_path, staged)
    else:
        fs.copy_file(out_path, staged)

    def unstage(reason: str) -> None:
        """Undo staging. The staged temp is the one file this module may unlink."""
        if staged_by_rename:
            # Put `out.mkv` back, or a resumed job finds `render.done` present and
            # the output gone, and fails in `verify` for reasons that look nothing
            # like the actual cause. Only unlink it if that could not be done.
            with contextlib.suppress(OSError):
                fs.rename(staged, out_path)
        if fs.exists(staged):
            fs.unlink(staged)
        log.warning("swap.unstaged", reason=reason, staged=str(staged))

    if not _sized(fs, staged, plan.out_size):
        # A short write is the classic ENOSPC outcome, and `disk_free` lies on a
        # fuse share, so the size is checked rather than trusted.
        unstage("staged file is the wrong size")
        raise StageError(NAME, f"staged file {staged} is not {plan.out_size} bytes")

    # --- permissions, before the file becomes visible under its real name
    mode_applied = owner_applied = False
    if plan.mode is not None:
        try:
            fs.chmod(staged, plan.mode)
            mode_applied = True
        except OSError as exc:
            warnings.append(f"could not copy permissions: {exc}")
    if plan.uid is not None and plan.gid is not None:
        try:
            fs.chown(staged, plan.uid, plan.gid)
            owner_applied = True
        except OSError as exc:
            # A gosu'd non-root process can only chown files it already owns. With
            # PUID/PGID and umask 0002 (§10) the ownership is already right, so this
            # is a fixup, not a guarantee.
            warnings.append(f"could not copy ownership: {exc}")
    # mtime is deliberately NOT copied: Jellyfin's scanner keys on it, and the
    # refresh stage that runs next depends on the file looking new.

    # --- the two renames
    fs.mkdirs(backup.parent)
    try:
        fs.rename(source, backup)
    except OSError as exc:
        unstage("could not move the original to the backup directory")
        if _is_exdev(exc) and not allow_cross_device_backup:
            # `preflight` says this when `same_device` saw the split; here the rename
            # is what discovered it, and the user needs the same sentence either way.
            raise StageError(NAME, _cross_device_message(source, backup)) from exc
        raise StageError(NAME, f"could not back up {source} to {backup}: {exc}") from exc

    try:
        fs.rename(staged, final)
    except OSError as exc:
        try:
            fs.rename(backup, source)
        except OSError as rollback_exc:
            fs.write_text(
                Path(f"{plan.staged_path}.broken"),
                f"install failed: {exc}\nrollback failed: {rollback_exc}\n",
            )
            raise SwapBrokenError(
                NAME,
                f"could not install {final} ({exc}) and could not put {source} back "
                f"from {backup} ({rollback_exc}). Both paths need a human.",
            ) from exc
        fs.unlink(staged)
        raise StageError(NAME, f"could not install {final}: {exc}") from exc

    # --- sidecars last: a subtitle problem must never cost a good video swap
    installed: list[SidecarSwap] = []
    for sidecar in plan.sidecars:
        done, problem = _swap_sidecar(sidecar, fs=fs)
        if problem:
            warnings.append(problem)
        if done:
            installed.append(sidecar)

    return SwapResult(
        original_path=plan.source_path,
        final_path=plan.final_path,
        backup_path=plan.backup_path,
        old_path=plan.source_path if plan.extension_changed else None,
        backup_size=plan.source_size,
        backup_sha1_prefix=_safe_fingerprint(backup, fs),
        out_size=plan.out_size,
        stage_via=plan.stage_via,
        backup_via=plan.backup_via,
        mode_applied=mode_applied,
        owner_applied=owner_applied,
        sidecars=installed,
        elapsed_s=round(time.monotonic() - started, 3),
        warnings=warnings,
    )


def _swap_sidecar(sidecar: SidecarSwap, *, fs: FsOps) -> tuple[bool, str | None]:
    original = Path(sidecar.original_path)
    backup = Path(sidecar.backup_path)
    staged = Path(sidecar.staged_path)
    if not fs.exists(staged):
        return False, f"redacted subtitle {staged} is missing"
    try:
        fs.mkdirs(backup.parent)
        fs.rename(original, backup)
    except OSError as exc:
        return False, f"could not back up {original}: {exc}"
    try:
        fs.copy_file(staged, original)
    except OSError as exc:
        try:
            fs.rename(backup, original)
        except OSError:
            return False, f"could not install {original} and could not restore it: {exc}"
        return False, f"could not install the redacted {original}: {exc}"
    return True, None


def write_ignore_marker(backups_root: Path, fs: FsOps | None = None) -> None:
    """`/backups` defaults to a directory *inside* the media share (§10), where a
    Jellyfin library scan would otherwise find every original we ever kept."""
    fs = fs or RealFs()
    marker = backups_root / IGNORE_MARKER
    if fs.exists(marker):
        return
    with contextlib.suppress(OSError):
        fs.mkdirs(backups_root)
        fs.write_text(marker, "VidCleaner backups. Not a media library.\n")


def _safe_fingerprint(path: Path, fs: FsOps) -> str:
    """The same first-8MB/last-8MB/size digest ``probe`` uses, so a ``backups`` row's
    ``sha1_prefix`` is directly comparable to the output's ``VIDCLEANER_SRC_FP`` tag."""
    try:
        fs.stat(path)
    except OSError:
        return ""
    try:
        return fingerprint(path)
    except OSError:
        return ""


# ----------------------------------------------------------------- restore


@dataclass(frozen=True, slots=True)
class RestorePlan:
    backup_path: Path
    target_path: Path
    """``media_items.path`` -- the file's *current* name, not the one it had when it
    was backed up. A ``Rename`` webhook may have moved it since."""
    displace_path: Path | None
    """The cleaned file to move out of the way. Never unlinked."""
    displace_to: Path | None = None
    """Where to move it. Defaults to beside itself as ``<name>.cleaned``, which is
    safe (an arr's disk scan enumerates by video extension) but leaves a
    source-sized file in the media share. Callers that know where ``/backups`` is
    should point this there instead."""
    expect_size: int = 0
    expect_sha1_prefix: str = ""
    mode: int | None = None


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_path: str
    backup_path: str
    displaced_path: str | None
    warnings: tuple[str, ...] = ()


def restore_backup(plan: RestorePlan, *, fs: FsOps | None = None) -> RestoreResult:
    """Put the original back and move the cleaned file out of the way.

    §9.3 says "restore originals", and the obvious implementation -- rename the
    backup to ``backups.original_path`` -- is wrong. After a ``Rename`` webhook that
    path is stale, so it would recreate the old filename *and* leave the cleaned
    file behind: two video files in one folder, which §3 says can make an arr adopt
    the wrong one. The target is therefore the item's current path.
    """
    fs = fs or RealFs()
    warnings: list[str] = []

    if not fs.exists(plan.backup_path):
        raise StageError(NAME, f"backup is missing: {plan.backup_path}")
    if plan.expect_size and fs.stat(plan.backup_path).st_size != plan.expect_size:
        raise StageError(NAME, f"backup {plan.backup_path} is not the size we recorded")
    if plan.expect_sha1_prefix:
        actual = _safe_fingerprint(plan.backup_path, fs)
        if actual and actual != plan.expect_sha1_prefix:
            raise StageError(NAME, f"backup {plan.backup_path} does not match its fingerprint")

    displaced: str | None = None
    if plan.displace_path is not None and fs.exists(plan.displace_path):
        target = _free_name(plan.displace_to or plan.displace_path, fs)
        fs.mkdirs(target.parent)
        try:
            fs.rename(plan.displace_path, target)
        except OSError as exc:
            if not _is_exdev(exc):
                raise StageError(NAME, f"could not move {plan.displace_path} aside: {exc}") from exc
            fs.copy_file(plan.displace_path, target)
            fs.unlink(plan.displace_path)  # our own cleaned output, not an original
        displaced = str(target)

    try:
        fs.rename(plan.backup_path, plan.target_path)
    except OSError as exc:
        if displaced is not None and plan.displace_path is not None:
            with contextlib.suppress(OSError):
                fs.rename(Path(displaced), plan.displace_path)
        raise StageError(NAME, f"could not restore {plan.target_path}: {exc}") from exc

    if plan.mode is not None:
        try:
            fs.chmod(plan.target_path, plan.mode)
        except OSError as exc:
            warnings.append(f"could not restore permissions: {exc}")

    return RestoreResult(
        restored_path=str(plan.target_path),
        backup_path=str(plan.backup_path),
        displaced_path=displaced,
        warnings=tuple(warnings),
    )


def _free_name(path: Path, fs: FsOps) -> Path:
    """``Foo.mkv`` -> ``Foo.mkv.cleaned``, then ``.cleaned.1`` and so on.

    The ``.cleaned`` suffix matters even in `/backups`: it is not a video
    extension, so nothing scanning for media will pick it up.
    """
    candidate = path.with_name(f"{path.name}.cleaned")
    n = 1
    while fs.exists(candidate):
        candidate = path.with_name(f"{path.name}.cleaned.{n}")
        n += 1
    return candidate


# ------------------------------------------------------------ reconciliation


def reconcile(session, job) -> str:
    """``claim.SWAP_RECONCILER``: what to do with a job killed mid-swap.

    Registered at import time by ``worker.runner``, so the queue does not have to
    import the pipeline (and through it ffmpeg) to recover a job.
    """
    from vidcleaner.config import get_settings  # noqa: PLC0415

    ws = Workspace.for_job(job.id, get_settings())
    if not ws.swap_plan_json.is_file():
        # The journal is written before the first rename, so its absence means the
        # library was never touched.
        return "requeue"
    try:
        plan = SwapPlan.read(ws.swap_plan_json)
    except Exception:  # noqa: BLE001
        return "failed"

    verdict = recover(plan)
    log.warning("swap.reconciled", job_id=job.id, action=verdict.action, detail=verdict.detail)
    if verdict.action == "committed":
        if verdict.result is not None:
            verdict.result.write(ws.swap_json)
            ws.mark_done(NAME)
        return "done"
    if verdict.action in ("redo", "rolled_back"):
        ws.clear_from("render" if verdict.action == "rolled_back" else NAME)
        return "requeue"
    return "failed"


# --------------------------------------------------------------------- stage


def run(ctx) -> None:
    probe = ProbeResult.read(ctx.ws.probe_json)
    render = RenderResult.read(ctx.ws.render_json)

    if not ctx.spec.in_place:
        raise StageError(NAME, "this job is not allowed to modify the library (in_place=False)")
    if ctx.spec.dry_run:
        raise StageError(NAME, "a dry run never touches the library")

    verify = VerifyResult.read(ctx.ws.verify_json) if ctx.ws.verify_json.is_file() else None
    if verify is None or not verify.ok:
        # §6 step 7 promises "Fail -> failed, library untouched".
        raise StageError(NAME, "verification did not pass; the library is left untouched")

    out_path = Path(render.out_path)
    fs = RealFs()

    # A crashed previous attempt is resolved from the journal before anything else.
    if ctx.ws.swap_plan_json.is_file():
        previous = SwapPlan.read(ctx.ws.swap_plan_json)
        verdict = recover(previous, fs=fs)
        ctx.log.info("swap.recovery", action=verdict.action, detail=verdict.detail)
        if verdict.action == "committed" and verdict.result is not None:
            verdict.result.write(ctx.ws.swap_json)
            return
        if verdict.action == "broken":
            raise SwapBrokenError(NAME, verdict.detail)
        if verdict.action == "stale":
            raise StaleSourceError(NAME, verdict.detail)
        if verdict.action == "rolled_back":
            raise StageError(
                NAME, f"{verdict.detail}; the render has been discarded and must be redone"
            )

    if not fs.exists(out_path):
        raise StageError(NAME, f"the rendered output is missing: {out_path}")

    plan = plan_swap(
        ctx.spec,
        probe,
        render,
        media_dir=ctx.deploy.media_dir,
        backups_dir=ctx.deploy.backups_dir,
        fs=fs,
    )
    preflight(
        plan,
        allow_cross_device_backup=ctx.settings.allow_cross_device_backup,
        fs=fs,
        arr_paths=_arr_paths(ctx),
    )

    write_ignore_marker(ctx.deploy.backups_dir, fs)

    # The journal, fsynced, after staging is planned and before anything moves.
    plan.write(ctx.ws.swap_plan_json, fsync=True)

    result = execute(
        plan,
        out_path=out_path,
        fs=fs,
        allow_cross_device_backup=ctx.settings.allow_cross_device_backup,
    )
    result.write(ctx.ws.swap_json)
    ctx.progress(NAME, 1.0)
    ctx.log.info(
        "swap.done",
        final=result.final_path,
        backup=result.backup_path,
        sidecars=len(result.sidecars),
        warnings=len(result.warnings),
        elapsed_s=result.elapsed_s,
    )


def _arr_paths(ctx) -> tuple[str, ...]:
    """The library folder, if we know it. Used only to refuse a `/backups` inside it."""
    getter = getattr(ctx, "arr_paths", None)
    return tuple(getter) if getter else ()


def load(ws: Workspace) -> SwapResult | None:
    return SwapResult.read(ws.swap_json) if ws.swap_json.is_file() else None
