"""Moving an existing install's originals out of the old hidden backups directory.

The interesting cases are all about *not* moving: a swap that crashed between its two
renames journalled the old path, and `swap.recover` decides what happened by the size
of the files at exactly those paths. Move one out from under an unresolved journal and
recovery reads "source gone, backup gone" and concludes `stale` -- a wrong answer that
cannot be walked back. So the guards get as many tests as the happy path does.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from tests.support.library import add_backup, add_job, make_movie
from vidcleaner.config import Settings, get_settings
from vidcleaner.db.models import Backup
from vidcleaner.db.session import session_scope
from vidcleaner.pipeline.swap import RealFs
from vidcleaner.worker.relocate import migrate_legacy_backups


@pytest.fixture
def relocatable(migrated: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """`backups_dir` at its derived default, so the legacy directory is a real source."""
    monkeypatch.delenv("VIDCLEANER_BACKUPS_DIR", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    return settings


def legacy_file(settings: Settings, relative: str, size: int = 512) -> Path:
    path = settings.legacy_backups_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"o" * size)
    return path


def run(settings: Settings, **kw):
    with session_scope() as session:
        return migrate_legacy_backups(session, settings, **kw)


def path_of(backup_id: int) -> str:
    with session_scope() as session:
        return session.get(Backup, backup_id).backup_path


# ------------------------------------------------------------------ the happy path


def test_the_tree_is_mirrored_and_the_rows_follow(relocatable: Settings) -> None:
    _title, item_id = make_movie()
    source = legacy_file(relocatable, "movies/Film (1999)/Film.mkv")
    sidecar = legacy_file(relocatable, "movies/Film (1999)/Film.en.srt", size=64)
    video_id = add_backup(item_id, backup_path=str(source))
    sidecar_id = add_backup(item_id, backup_path=str(sidecar))

    report = run(relocatable)

    moved = relocatable.backups_dir / "movies/Film (1999)/Film.mkv"
    assert report.blocked is None
    assert report.moved == 2
    assert report.rows_updated == 2
    assert moved.read_bytes() == b"o" * 512
    assert not source.exists()
    assert path_of(video_id) == str(moved)
    assert path_of(sidecar_id) == str(relocatable.backups_dir / "movies/Film (1999)/Film.en.srt")


def test_the_odd_shapes_swap_invents_survive_the_move(relocatable: Settings) -> None:
    """`_abs/` for a file outside /media and `.vc-<job8>` for a second clean.

    The destination is a *relative remap*, never a re-derivation through
    `backup_path_for` -- which would drop both.
    """
    _title, item_id = make_movie()
    outside = legacy_file(relocatable, "_abs/srv/media/Film.mkv")
    second = legacy_file(relocatable, "movies/Film (1999)/Film.vc-abcd1234.mkv")
    outside_id = add_backup(item_id, backup_path=str(outside))
    second_id = add_backup(item_id, backup_path=str(second))

    run(relocatable)

    assert path_of(outside_id) == str(relocatable.backups_dir / "_abs/srv/media/Film.mkv")
    assert path_of(second_id).endswith("Film.vc-abcd1234.mkv")


def test_the_emptied_legacy_directory_and_its_markers_are_removed(
    relocatable: Settings,
) -> None:
    legacy_file(relocatable, "movies/Film.mkv")
    (relocatable.legacy_backups_dir / ".ignore").write_text("x\n")

    report = run(relocatable)

    assert report.legacy_removed is True
    assert not relocatable.legacy_backups_dir.exists()
    assert (relocatable.backups_dir / ".ignore").is_file()
    assert (relocatable.backups_dir / "README.txt").is_file()


def test_our_own_markers_are_not_treated_as_originals(relocatable: Settings) -> None:
    """The destination usually already has its `.ignore`, written by the first swap.

    Moving the legacy one would collide with it -- and a collision is a skip, which
    leaves the legacy directory in place forever. Found by running the thing, not by
    review: the first version counted the marker as a third moved "original".
    """
    legacy_file(relocatable, "movies/Film.mkv")
    (relocatable.legacy_backups_dir / ".ignore").write_text("x\n")
    (relocatable.legacy_backups_dir / "README.txt").write_text("x\n")
    (relocatable.backups_dir / ".ignore").write_text("x\n")
    (relocatable.backups_dir / "README.txt").write_text("x\n")

    report = run(relocatable)

    assert report.moved == 1
    assert report.skipped == ()
    assert report.legacy_removed is True


def test_a_second_run_does_nothing(relocatable: Settings) -> None:
    legacy_file(relocatable, "movies/Film.mkv")
    run(relocatable)

    report = run(relocatable)

    assert report.moved == 0
    assert report.blocked == "no legacy directory"


def test_a_file_with_no_row_still_moves(relocatable: Settings) -> None:
    """A crash between a rename and its commit leaves one. It must not be stranded
    in a directory that is about to be deleted; `reconcile_backups` adopts it at the
    new path exactly as it would have at the old one."""
    legacy_file(relocatable, "tv/Show/S01E01.mkv")

    report = run(relocatable)

    assert report.moved == 1
    assert report.rows_updated == 0
    assert (relocatable.backups_dir / "tv/Show/S01E01.mkv").is_file()


# ------------------------------------------------------------------ the refusals


def test_originals_follow_a_deliberate_backups_dir_too(migrated: Settings) -> None:
    """An operator who points `VIDCLEANER_BACKUPS_DIR` somewhere of their own still
    gets the old directory emptied into it. Stranding the originals in a directory
    nothing reads any more would be the one outcome worse than not moving them.

    The conftest sets exactly such a path, so `migrated` rather than `relocatable`.
    """
    legacy_file(migrated, "movies/Film.mkv")

    report = run(migrated)

    assert report.moved == 1
    assert (migrated.backups_dir / "movies/Film.mkv").is_file()


def test_a_collision_is_never_overwritten(relocatable: Settings) -> None:
    _title, item_id = make_movie()
    source = legacy_file(relocatable, "movies/Film.mkv")
    backup_id = add_backup(item_id, backup_path=str(source))
    destination = relocatable.backups_dir / "movies/Film.mkv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"older original")

    report = run(relocatable)

    assert report.moved == 0
    assert report.skipped and "already exists" in report.skipped[0]
    assert destination.read_bytes() == b"older original"
    assert source.exists()
    assert path_of(backup_id) == str(source)
    assert report.legacy_removed is False


def test_cross_device_aborts_rather_than_copying(relocatable: Settings) -> None:
    """Copy-and-delete of a library original is reserved to nobody at all."""

    class ExdevFs(RealFs):
        def rename(self, src: Path, dst: Path) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

    _title, item_id = make_movie()
    source = legacy_file(relocatable, "movies/Film.mkv")
    backup_id = add_backup(item_id, backup_path=str(source))

    report = run(relocatable, fs=ExdevFs())

    assert report.moved == 0
    assert report.blocked is not None
    assert str(relocatable.legacy_backups_dir) in report.blocked
    assert source.exists()
    assert path_of(backup_id) == str(source)


def test_one_unreadable_file_does_not_cost_the_others(relocatable: Settings) -> None:
    class PickyFs(RealFs):
        def rename(self, src: Path, dst: Path) -> None:
            if src.name == "Locked.mkv":
                raise OSError(errno.EACCES, "Permission denied")
            super().rename(src, dst)

    legacy_file(relocatable, "movies/Locked.mkv")
    legacy_file(relocatable, "movies/Fine.mkv")

    report = run(relocatable, fs=PickyFs())

    assert report.moved == 1
    assert (relocatable.backups_dir / "movies/Fine.mkv").is_file()
    assert (relocatable.legacy_backups_dir / "movies/Locked.mkv").is_file()
    assert report.legacy_removed is False


def test_a_running_job_defers_the_whole_thing(relocatable: Settings) -> None:
    _title, item_id = make_movie()
    source = legacy_file(relocatable, "movies/Film.mkv")
    add_job(item_id, state="swapping")

    report = run(relocatable)

    assert report.moved == 0
    assert report.blocked is not None and "still running" in report.blocked
    assert source.exists()


def test_an_unresolved_swap_journal_defers_the_whole_thing(relocatable: Settings) -> None:
    """The guard that exists so `swap.recover` is never asked about a moved path.

    Walked from `/work` rather than the job table, because a `vidcleaner clean
    --in-place` run writes the same journal with no job row behind it.
    """
    source = legacy_file(relocatable, "movies/Film.mkv")
    work = relocatable.work_dir / "9f1c0d2e-cli-run"
    work.mkdir(parents=True)
    (work / "swap.plan.json").write_text("{}")

    report = run(relocatable)

    assert report.moved == 0
    assert report.blocked is not None and "unresolved swap journal" in report.blocked
    assert source.exists()

    # `swap.json` is written for every settled outcome, so its arrival unblocks us.
    (work / "swap.json").write_text("{}")
    assert run(relocatable).moved == 1


# ------------------------------------------------ the barrier the relocation needs


def test_reconcile_refuses_while_originals_are_still_being_moved(
    relocatable: Settings,
) -> None:
    """The pairing that would otherwise put a thirty-day deletion clock on an original.

    Mid-relocation a row can name the old path for a file that is already at the new
    one, and `reconcile_backups` reads that as two separate facts: the row's file is
    gone (mark it `purged`) and the new file has no row (adopt it as `orphaned`, with
    a clock). `purge_backups` then deletes the only copy a month later. The legacy
    directory exists for the whole of a relocation, so it is the barrier.
    """
    from vidcleaner.pipeline.persist import reconcile_backups

    _title, item_id = make_movie()
    stale_row = legacy_file(relocatable, "movies/Film.mkv")
    backup_id = add_backup(item_id, backup_path=str(stale_row))

    with session_scope() as session:
        report = reconcile_backups(
            session,
            relocatable.backups_dir,
            legacy_dir=relocatable.legacy_backups_dir,
        )

    assert report.skipped is True
    assert report.adopted == 0 and report.purged == 0
    with session_scope() as session:
        assert session.get(Backup, backup_id).state == "kept"

    # Once the move is finished the directory is gone and the backstop resumes.
    run(relocatable)
    with session_scope() as session:
        assert (
            reconcile_backups(
                session,
                relocatable.backups_dir,
                legacy_dir=relocatable.legacy_backups_dir,
            ).skipped
            is False
        )
