"""The swap transaction's failure modes, through an injected filesystem.

Every case here is one that has to work correctly the first time it happens for
real -- a full disk, a share on a different device, a permission the container does
not have, a rename that fails halfway. A fake `FsOps` is the only way to reach them
deterministically; waiting for a real ENOSPC is not a test strategy.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from vidcleaner.pipeline.artifacts import SwapPlan
from vidcleaner.pipeline.stages import StageError, SwapBrokenError
from vidcleaner.pipeline.swap import RealFs, execute

SOURCE_SIZE = 4096
OUT_SIZE = 5000


#: Which rename is which, by where it comes from and where it goes. Keyed on full
#: paths rather than basenames because the backup deliberately keeps the source's
#: name -- so `S01E01.mkv` names both sides of the first rename.
STEPS = {
    "stage": ("out.mkv", ".vidcleaner."),
    "backup": ("/media/", "/backups/"),
    "install": (".vidcleaner.", "/media/"),
    "rollback": ("/backups/", "/media/"),
    "unstage": (".vidcleaner.", "out.mkv"),
}


class FakeFs(RealFs):
    """A real filesystem with programmable failures and a call log."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail_steps: dict[str, OSError] = {}
        self.fail_copy_steps: dict[str, OSError] = {}
        self.fail_chown: OSError | None = None
        self.fail_chmod: OSError | None = None
        self.short_copy = False
        self.free_bytes: int | None = None

    def fail(self, step: str, exc: OSError) -> None:
        self.fail_steps[step] = exc

    def fail_copying(self, step: str, exc: OSError) -> None:
        self.fail_copy_steps[step] = exc

    def _step(self, src: Path, dst: Path) -> str | None:
        for name, (want_src, want_dst) in STEPS.items():
            if want_src in str(src) and want_dst in str(dst):
                return name
        return None

    def rename(self, src: Path, dst: Path) -> None:
        self.calls.append(("rename", str(src), str(dst)))
        exc = self.fail_steps.get(self._step(src, dst) or "")
        if exc is not None:
            raise exc
        super().rename(src, dst)

    def copy_file(self, src: Path, dst: Path) -> None:
        self.calls.append(("copy", str(src), str(dst)))
        exc = self.fail_copy_steps.get(self._step(src, dst) or "")
        if exc is not None:
            raise exc
        if self.short_copy:
            dst.write_bytes(src.read_bytes()[: max(0, src.stat().st_size - 10)])
            return
        super().copy_file(src, dst)

    def unlink(self, path: Path) -> None:
        self.calls.append(("unlink", str(path), None))
        super().unlink(path)

    def chown(self, path: Path, uid: int, gid: int) -> None:
        if self.fail_chown is not None:
            raise self.fail_chown
        super().chown(path, uid, gid)

    def chmod(self, path: Path, mode: int) -> None:
        if self.fail_chmod is not None:
            raise self.fail_chmod
        super().chmod(path, mode)

    def disk_free(self, path: Path) -> int:
        return self.free_bytes if self.free_bytes is not None else super().disk_free(path)

    def renames(self) -> list[tuple[str, str]]:
        return [(Path(a).name, Path(b).name) for kind, a, b in self.calls if kind == "rename"]


@pytest.fixture
def world(tmp_path: Path):
    """A library, a backups dir, a work dir with a rendered output, and a plan."""

    class World:
        def __init__(self) -> None:
            self.fs = FakeFs()
            self.folder = tmp_path / "media" / "tv" / "Show"
            self.folder.mkdir(parents=True)
            self.source = self.folder / "S01E01.mkv"
            self.source.write_bytes(b"o" * SOURCE_SIZE)
            self.backup = tmp_path / "backups" / "tv" / "Show" / "S01E01.mkv"
            self.work = tmp_path / "work"
            self.work.mkdir()
            self.out = self.work / "out.mkv"
            self.out.write_bytes(b"c" * OUT_SIZE)

        def plan(self, **kw) -> SwapPlan:
            values = {
                "job_id": "job-1",
                "source_path": str(self.source),
                "final_path": str(self.source),
                "backup_path": str(self.backup),
                "staged_path": str(self.folder / ".vidcleaner.S01E01.mkv.tmp"),
                "source_size": SOURCE_SIZE,
                "source_inode": self.source.stat().st_ino,
                "out_size": OUT_SIZE,
                "mode": 0o644,
                "uid": os.getuid(),
                "gid": os.getgid(),
            }
            values.update(kw)
            return SwapPlan(**values)

        def video_files(self) -> list[str]:
            return sorted(p.name for p in self.folder.iterdir() if p.suffix == ".mkv")

    return World()


# ------------------------------------------------------------------ happy path


def test_the_original_is_moved_and_the_clean_file_installed(world) -> None:
    result = execute(world.plan(), out_path=world.out, fs=world.fs)

    assert world.source.read_bytes() == b"c" * OUT_SIZE
    assert world.backup.read_bytes() == b"o" * SOURCE_SIZE
    assert result.backup_sha1_prefix
    assert result.mode_applied and result.owner_applied
    assert result.warnings == []
    # No unlink of a library file, ever.
    assert [c for c in world.fs.calls if c[0] == "unlink"] == []


def test_the_original_is_moved_out_before_the_clean_file_arrives(world) -> None:
    """§3: a stray sibling video file can be adopted by Sonarr as *the* file, so
    the folder must never hold two. It may briefly hold none."""
    execute(world.plan(), out_path=world.out, fs=world.fs)
    order = world.fs.renames()
    assert order == [
        ("out.mkv", ".vidcleaner.S01E01.mkv.tmp"),
        ("S01E01.mkv", "S01E01.mkv"),
        (".vidcleaner.S01E01.mkv.tmp", "S01E01.mkv"),
    ]
    assert world.video_files() == ["S01E01.mkv"]


def test_an_mp4_leaves_exactly_one_video_file(world) -> None:
    mp4 = world.folder / "S01E01.mp4"
    world.source.rename(mp4)
    world.source = mp4
    plan = world.plan(
        final_path=str(world.folder / "S01E01.mkv"),
        extension_changed=True,
    )
    result = execute(plan, out_path=world.out, fs=world.fs)

    assert sorted(p.name for p in world.folder.iterdir()) == ["S01E01.mkv"]
    assert result.old_path == str(mp4), "refresh needs the old name for Jellyfin"


def test_the_modification_time_is_deliberately_not_copied(world) -> None:
    """Jellyfin's scanner keys on mtime, and `refresh` runs right after this."""
    old_mtime = world.source.stat().st_mtime
    os.utime(world.source, (old_mtime - 100_000, old_mtime - 100_000))
    plan = world.plan(source_inode=world.source.stat().st_ino)
    execute(plan, out_path=world.out, fs=world.fs)
    assert world.source.stat().st_mtime > old_mtime - 100_000


# ------------------------------------------------------------------- staging


def test_a_cross_device_output_falls_back_to_copying(world) -> None:
    """`st_dev` only plans; unraid's shfs reports one device across several disks."""
    world.fs.fail("stage", OSError(errno.EXDEV, "cross-device link"))
    execute(world.plan(), out_path=world.out, fs=world.fs)
    assert world.source.read_bytes() == b"c" * OUT_SIZE
    assert ("copy", str(world.out), str(world.folder / ".vidcleaner.S01E01.mkv.tmp")) in [
        (k, a, b) for k, a, b in world.fs.calls
    ]


def test_a_full_disk_leaves_the_library_untouched(world) -> None:
    world.fs.fail("stage", OSError(errno.EXDEV, "cross-device link"))
    world.fs.fail_copying("stage", OSError(errno.ENOSPC, "no space left on device"))
    with pytest.raises(OSError, match="no space"):
        execute(world.plan(), out_path=world.out, fs=world.fs)
    assert world.source.read_bytes() == b"o" * SOURCE_SIZE
    assert not world.backup.exists()


def test_a_short_copy_is_caught_and_the_output_restored(world) -> None:
    """`disk_free` lies on a fuse share, so the staged size is checked."""
    world.fs.fail("stage", OSError(errno.EXDEV, "cross-device link"))
    world.fs.short_copy = True
    with pytest.raises(StageError, match="bytes"):
        execute(world.plan(), out_path=world.out, fs=world.fs)
    assert world.source.read_bytes() == b"o" * SOURCE_SIZE
    assert world.video_files() == ["S01E01.mkv"]


def test_a_failed_stage_puts_the_render_back_so_a_retry_is_cheap(world) -> None:
    """Otherwise a resumed job finds `render.done` and no `out.mkv`, then fails in
    `verify` for reasons that look nothing like the real cause."""
    world.fs.fail("backup", OSError(errno.EACCES, "permission denied"))
    with pytest.raises(StageError):
        execute(world.plan(), out_path=world.out, fs=world.fs)
    assert world.out.is_file() and world.out.stat().st_size == OUT_SIZE
    assert world.source.read_bytes() == b"o" * SOURCE_SIZE


# --------------------------------------------------------------- permissions


def test_an_unpermitted_chown_warns_and_the_swap_proceeds(world) -> None:
    """A gosu'd non-root process can only chown files it already owns."""
    world.fs.fail_chown = OSError(errno.EPERM, "operation not permitted")
    result = execute(world.plan(), out_path=world.out, fs=world.fs)
    assert result.owner_applied is False
    assert any("ownership" in w for w in result.warnings)
    assert world.source.read_bytes() == b"c" * OUT_SIZE


def test_an_unpermitted_chmod_warns_and_the_swap_proceeds(world) -> None:
    world.fs.fail_chmod = OSError(errno.EPERM, "operation not permitted")
    result = execute(world.plan(), out_path=world.out, fs=world.fs)
    assert result.mode_applied is False
    assert any("permissions" in w for w in result.warnings)


# ------------------------------------------------------------ the two renames


def test_a_failed_install_rolls_the_original_back(world) -> None:
    world.fs.fail("install", OSError(errno.EIO, "input/output error"))
    with pytest.raises(StageError, match="could not install"):
        execute(world.plan(), out_path=world.out, fs=world.fs)

    assert world.source.read_bytes() == b"o" * SOURCE_SIZE
    assert not world.backup.exists()
    assert world.video_files() == ["S01E01.mkv"]


def test_a_failed_rollback_is_the_one_unrecoverable_state(world) -> None:
    """The install fails *and* putting the original back fails. Nothing automated
    can fix this, so it must be loud rather than retried."""
    world.fs.fail("install", OSError(errno.EIO, "input/output error"))
    world.fs.fail("rollback", OSError(errno.EIO, "input/output error"))

    with pytest.raises(SwapBrokenError, match="need a human") as caught:
        execute(world.plan(), out_path=world.out, fs=world.fs)

    message = str(caught.value)
    assert str(world.source) in message and str(world.backup) in message
    assert Path(str(world.folder / ".vidcleaner.S01E01.mkv.tmp") + ".broken").is_file()


# ---------------------------------------------------------------- sidecars


def test_sidecars_are_installed_after_the_video(world, tmp_path: Path) -> None:
    from vidcleaner.pipeline.artifacts import SidecarSwap

    original = world.folder / "S01E01.srt"
    original.write_text("Oh shit.")
    staged = tmp_path / "work" / "redacted" / "side_0.srt"
    staged.parent.mkdir(parents=True)
    staged.write_text("Oh ****.")
    backup = tmp_path / "backups" / "tv" / "Show" / "S01E01.srt"

    plan = world.plan(
        sidecars=[
            SidecarSwap(
                original_path=str(original),
                backup_path=str(backup),
                staged_path=str(staged),
                replacements=1,
            )
        ]
    )
    result = execute(plan, out_path=world.out, fs=world.fs)

    assert original.read_text() == "Oh ****."
    assert backup.read_text() == "Oh shit."
    assert len(result.sidecars) == 1
    # The video was installed first: a subtitle problem must not cost a good swap.
    names = world.fs.renames()
    assert names.index((".vidcleaner.S01E01.mkv.tmp", "S01E01.mkv")) < names.index(
        ("S01E01.srt", "S01E01.srt")
    )


def test_a_missing_redacted_sidecar_only_warns(world, tmp_path: Path) -> None:
    from vidcleaner.pipeline.artifacts import SidecarSwap

    original = world.folder / "S01E01.srt"
    original.write_text("Oh shit.")
    plan = world.plan(
        sidecars=[
            SidecarSwap(
                original_path=str(original),
                backup_path=str(tmp_path / "backups" / "S01E01.srt"),
                staged_path=str(tmp_path / "work" / "gone.srt"),
            )
        ]
    )
    result = execute(plan, out_path=world.out, fs=world.fs)

    assert world.source.read_bytes() == b"c" * OUT_SIZE, "the video swap still succeeded"
    assert result.sidecars == []
    assert any("missing" in w for w in result.warnings)
    assert original.read_text() == "Oh shit."
