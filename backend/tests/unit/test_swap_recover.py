"""Every state a crashed swap can leave behind, and what we do about it.

Two renames cannot be made atomic. §6 step 8's "on failure rename(backup ->
original)" only covers an *exception*: a power loss between the renames leaves a
library folder with no video file, and without the fsynced intent journal nothing on
disk says why. `recover` reads the journal and resolves the state.

Crash timing cannot be tested by luck -- you cannot reliably `docker kill` between
two renames -- but it can be tested exhaustively by constructing each reachable state
as plain files. That is what this file does, which is why it is the most valuable test
in the milestone.

The decision is driven by **size**, not mere existence, because that is the only
question that distinguishes an MKV swapped in place (where source and final are one
path) from an MP4 that became an MKV -- and it also catches a short copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.pipeline.artifacts import SwapPlan
from vidcleaner.pipeline.swap import recover

SOURCE_SIZE = 4096
OUT_SIZE = 5000
ORIGINAL = b"o" * SOURCE_SIZE
CLEANED = b"c" * OUT_SIZE


@pytest.fixture
def world(tmp_path: Path):
    class World:
        def __init__(self) -> None:
            self.folder = tmp_path / "media" / "tv" / "Show"
            self.folder.mkdir(parents=True)
            self.backups = tmp_path / "backups" / "tv" / "Show"
            self.backups.mkdir(parents=True)
            self.source = self.folder / "S01E01.mkv"
            self.backup = self.backups / "S01E01.mkv"
            self.staged = self.folder / ".vidcleaner.S01E01.mkv.tmp"

        def plan(self, *, mp4: bool = False) -> SwapPlan:
            source = self.folder / ("S01E01.mp4" if mp4 else "S01E01.mkv")
            return SwapPlan(
                job_id="job-1",
                source_path=str(source),
                final_path=str(self.folder / "S01E01.mkv"),
                backup_path=str(self.backups / source.name),
                staged_path=str(self.staged),
                source_size=SOURCE_SIZE,
                out_size=OUT_SIZE,
                extension_changed=mp4,
            )

    return World()


# ---------------------------------------------------- the seven reachable states


def test_nothing_committed_is_a_clean_redo(world) -> None:
    world.source.write_bytes(ORIGINAL)
    verdict = recover(world.plan())
    assert verdict.action == "redo"
    assert world.source.read_bytes() == ORIGINAL


def test_a_leftover_staging_temp_is_removed_before_redoing(world) -> None:
    world.source.write_bytes(ORIGINAL)
    world.staged.write_bytes(b"partial")
    assert recover(world.plan()).action == "redo"
    assert not world.staged.exists()


def test_a_good_staged_file_rolls_forward(world) -> None:
    """Those bytes already passed `verify`. Rolling back would throw away a good
    render for nothing."""
    world.backup.write_bytes(ORIGINAL)
    world.staged.write_bytes(CLEANED)

    verdict = recover(world.plan())
    assert verdict.action == "committed"
    assert verdict.detail == "rolled_forward"
    assert world.source.read_bytes() == CLEANED
    assert world.backup.read_bytes() == ORIGINAL
    assert verdict.result is not None and verdict.result.recovered_from == "rolled_forward"


def test_no_staged_file_rolls_back(world) -> None:
    world.backup.write_bytes(ORIGINAL)

    verdict = recover(world.plan())
    assert verdict.action == "rolled_back"
    assert world.source.read_bytes() == ORIGINAL
    assert not world.backup.exists()


def test_a_short_staged_file_rolls_back(world) -> None:
    """The classic ENOSPC outcome. Existence is not enough: the size decides."""
    world.backup.write_bytes(ORIGINAL)
    world.staged.write_bytes(CLEANED[:-10])

    verdict = recover(world.plan())
    assert verdict.action == "rolled_back"
    assert world.source.read_bytes() == ORIGINAL
    assert not world.staged.exists()


def test_a_committed_swap_is_recognised_and_left_alone(world) -> None:
    world.source.write_bytes(CLEANED)
    world.backup.write_bytes(ORIGINAL)

    verdict = recover(world.plan())
    assert verdict.action == "committed"
    assert verdict.detail == "committed"
    assert world.source.read_bytes() == CLEANED
    assert verdict.result is not None
    assert verdict.result.warnings == []


def test_a_committed_swap_whose_backup_a_human_moved_still_reads_as_committed(world) -> None:
    world.source.write_bytes(CLEANED)

    verdict = recover(world.plan())
    assert verdict.action == "committed"
    assert verdict.result is not None
    assert any("backup is missing" in w for w in verdict.result.warnings)


def test_the_original_and_its_backup_both_existing_is_refused(world) -> None:
    """Only reachable if rename #1 behaved as a copy. We cannot tell which file is
    authoritative, so we stop instead of guessing."""
    world.source.write_bytes(ORIGINAL)
    world.backup.write_bytes(ORIGINAL)

    verdict = recover(world.plan())
    assert verdict.action == "broken"
    assert "unexpected state" in verdict.detail


def test_everything_gone_is_stale_not_broken(world) -> None:
    """The library file was deleted or moved by something else entirely; §6's
    "path vanished" path applies, and that is recovery rather than failure."""
    verdict = recover(world.plan())
    assert verdict.action == "stale"


# ------------------------------------------------------------ the mp4 variant


def test_an_interrupted_mp4_swap_rolls_forward_to_the_mkv(world) -> None:
    plan = world.plan(mp4=True)
    Path(plan.backup_path).write_bytes(ORIGINAL)
    world.staged.write_bytes(CLEANED)

    verdict = recover(plan)
    assert verdict.action == "committed"
    assert Path(plan.final_path).read_bytes() == CLEANED
    assert not Path(plan.source_path).exists(), "exactly one video file in the folder"
    assert verdict.result is not None
    assert verdict.result.old_path == plan.source_path, "refresh must tell Jellyfin"


def test_an_interrupted_mp4_swap_rolls_back_to_the_mp4(world) -> None:
    plan = world.plan(mp4=True)
    Path(plan.backup_path).write_bytes(ORIGINAL)

    verdict = recover(plan)
    assert verdict.action == "rolled_back"
    assert Path(plan.source_path).read_bytes() == ORIGINAL
    assert not Path(plan.final_path).exists()


def test_a_committed_mp4_swap_is_recognised(world) -> None:
    plan = world.plan(mp4=True)
    Path(plan.final_path).write_bytes(CLEANED)
    Path(plan.backup_path).write_bytes(ORIGINAL)

    verdict = recover(plan)
    assert verdict.action == "committed"
    assert Path(plan.source_path).exists() is False


# ----------------------------------------------------------------- idempotence


def test_recovering_twice_changes_nothing(world) -> None:
    world.backup.write_bytes(ORIGINAL)
    world.staged.write_bytes(CLEANED)

    first = recover(world.plan())
    second = recover(world.plan())
    assert first.action == second.action == "committed"
    assert world.source.read_bytes() == CLEANED


def test_a_forced_rerun_of_a_finished_swap_is_a_no_op(world) -> None:
    """Otherwise a `--force` reprocess would try to back up our own output."""
    world.source.write_bytes(CLEANED)
    world.backup.write_bytes(ORIGINAL)

    assert recover(world.plan()).action == "committed"
    assert world.backup.read_bytes() == ORIGINAL, "the real original is still the backup"
