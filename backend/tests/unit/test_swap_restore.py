"""Putting the original back (§9.3's "restore originals").

The obvious implementation -- rename the backup to `backups.original_path` -- is
wrong, and the tests below are built to demonstrate why rather than assert it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.pipeline.stages import StageError
from vidcleaner.pipeline.swap import RealFs, RestorePlan, restore_backup

ORIGINAL = b"o" * 4096
CLEANED = b"c" * 5000


@pytest.fixture
def world(tmp_path: Path):
    class World:
        def __init__(self) -> None:
            self.folder = tmp_path / "media" / "tv" / "Show"
            self.folder.mkdir(parents=True)
            self.backups = tmp_path / "backups" / "tv" / "Show"
            self.backups.mkdir(parents=True)
            self.backup = self.backups / "S01E01.mkv"
            self.backup.write_bytes(ORIGINAL)

        def cleaned(self, name: str = "S01E01.mkv") -> Path:
            path = self.folder / name
            path.write_bytes(CLEANED)
            return path

    return World()


def test_the_original_comes_back_and_the_clean_file_is_displaced(world) -> None:
    cleaned = world.cleaned()
    result = restore_backup(
        RestorePlan(
            backup_path=world.backup,
            target_path=cleaned,
            displace_path=cleaned,
            expect_size=len(ORIGINAL),
        ),
        fs=RealFs(),
    )

    assert cleaned.read_bytes() == ORIGINAL
    assert result.displaced_path is not None
    # Never unlinked -- the cleaned file is moved aside, not deleted.
    assert Path(result.displaced_path).read_bytes() == CLEANED
    assert not world.backup.exists()


def test_restoring_targets_the_current_path_not_the_one_we_backed_up(world) -> None:
    """A `Rename` webhook may have moved the file since the swap. Restoring to
    `backups.original_path` would recreate the old filename *and* leave the cleaned
    file behind: two video files in one folder, which §3 says can make an arr adopt
    the wrong one."""
    renamed = world.cleaned("S01E01 - New Title.mkv")

    restore_backup(
        RestorePlan(backup_path=world.backup, target_path=renamed, displace_path=renamed),
        fs=RealFs(),
    )

    assert renamed.read_bytes() == ORIGINAL
    assert not (world.folder / "S01E01.mkv").exists(), "the stale name is not recreated"
    videos = sorted(p.name for p in world.folder.iterdir() if p.suffix == ".mkv")
    assert videos == ["S01E01 - New Title.mkv"]


def test_an_mp4_restore_removes_the_mkv_so_one_video_file_remains(world) -> None:
    """After an MP4 -> MKV swap the library holds `S01E01.mkv`; putting the `.mp4`
    back without displacing it would leave the arr two files to choose from."""
    mp4_backup = world.backups / "S01E01.mp4"
    mp4_backup.write_bytes(ORIGINAL)
    cleaned_mkv = world.cleaned()

    restore_backup(
        RestorePlan(
            backup_path=mp4_backup,
            target_path=world.folder / "S01E01.mp4",
            displace_path=cleaned_mkv,
        ),
        fs=RealFs(),
    )

    remaining = sorted(p.name for p in world.folder.iterdir() if p.suffix in (".mkv", ".mp4"))
    assert remaining == ["S01E01.mp4"]


def test_nothing_to_displace_is_fine(world) -> None:
    target = world.folder / "S01E01.mkv"
    result = restore_backup(
        RestorePlan(backup_path=world.backup, target_path=target, displace_path=None),
        fs=RealFs(),
    )
    assert target.read_bytes() == ORIGINAL
    assert result.displaced_path is None


def test_a_missing_backup_is_refused(world) -> None:
    world.backup.unlink()
    with pytest.raises(StageError, match="missing"):
        restore_backup(
            RestorePlan(
                backup_path=world.backup,
                target_path=world.folder / "S01E01.mkv",
                displace_path=None,
            ),
            fs=RealFs(),
        )


def test_a_backup_of_the_wrong_size_is_refused(world) -> None:
    cleaned = world.cleaned()
    with pytest.raises(StageError, match="size"):
        restore_backup(
            RestorePlan(
                backup_path=world.backup,
                target_path=cleaned,
                displace_path=cleaned,
                expect_size=999,
            ),
            fs=RealFs(),
        )
    assert cleaned.read_bytes() == CLEANED, "the library is untouched by a refusal"


def test_a_backup_that_fails_its_fingerprint_is_refused(world) -> None:
    with pytest.raises(StageError, match="fingerprint"):
        restore_backup(
            RestorePlan(
                backup_path=world.backup,
                target_path=world.folder / "S01E01.mkv",
                displace_path=None,
                expect_sha1_prefix="not-the-right-digest",
            ),
            fs=RealFs(),
        )


def test_a_second_displacement_does_not_overwrite_the_first(world) -> None:
    cleaned = world.cleaned()
    (world.folder / "S01E01.mkv.cleaned").write_bytes(b"an earlier one")

    result = restore_backup(
        RestorePlan(backup_path=world.backup, target_path=cleaned, displace_path=cleaned),
        fs=RealFs(),
    )
    assert result.displaced_path is not None
    assert result.displaced_path.endswith(".cleaned.1")
    assert (world.folder / "S01E01.mkv.cleaned").read_bytes() == b"an earlier one"


def test_the_cleaned_file_can_be_displaced_to_another_directory(world, tmp_path: Path) -> None:
    """Leaving a source-sized `.cleaned` file in the media share works (nothing
    scans for that extension) but nothing would ever tidy it up either."""
    cleaned = world.cleaned()
    result = restore_backup(
        RestorePlan(
            backup_path=world.backup,
            target_path=cleaned,
            displace_path=cleaned,
            displace_to=world.backups / "S01E01.mkv",
        ),
        fs=RealFs(),
    )
    assert result.displaced_path == str(world.backups / "S01E01.mkv.cleaned")
    assert (world.backups / "S01E01.mkv.cleaned").read_bytes() == CLEANED
    assert sorted(p.name for p in world.folder.iterdir()) == ["S01E01.mkv"]
