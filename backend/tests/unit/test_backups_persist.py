"""`backups` rows, restore bookkeeping, and the reconciler (PLAN.md §5, §9.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from vidcleaner.config import Settings
from vidcleaner.db.models import Backup, MediaItem, Title
from vidcleaner.db.session import session_scope
from vidcleaner.pipeline.artifacts import SidecarSwap, SwapResult
from vidcleaner.pipeline.persist import (
    persist_swap,
    reconcile_backups,
    restore_item,
)
from vidcleaner.pipeline.probe import fingerprint

ORIGINAL = b"o" * 4096
CLEANED = b"c" * 5000


@pytest.fixture
def library(tmp_path: Path, migrated: Settings):
    """A tracked, already-swapped episode: clean file in place, original in /backups."""

    class Library:
        def __init__(self) -> None:
            self.folder = tmp_path / "media" / "tv" / "Show"
            self.folder.mkdir(parents=True)
            self.backups = tmp_path / "backups" / "tv" / "Show"
            self.backups.mkdir(parents=True)
            self.final = self.folder / "S01E01.mkv"
            self.final.write_bytes(CLEANED)
            self.backup = self.backups / "S01E01.mkv"
            self.backup.write_bytes(ORIGINAL)
            with session_scope() as session:
                title = Title(kind="series", arr_id=1, title="Show", enabled=True)
                session.add(title)
                session.flush()
                item = MediaItem(
                    title_id=title.id,
                    kind="episode",
                    season=1,
                    episode=1,
                    path=str(self.folder / "S01E01.mkv"),
                    status="pending",
                )
                session.add(item)
                session.flush()
                self.item_id = item.id

        def swap_result(self, **kw) -> SwapResult:
            values = {
                "original_path": str(self.folder / "S01E01.mkv"),
                "final_path": str(self.final),
                "backup_path": str(self.backup),
                "backup_size": len(ORIGINAL),
                # The real digest: `restore_backup` verifies it before moving
                # anything, so a placeholder here would only test the refusal.
                "backup_sha1_prefix": fingerprint(self.backup),
                "out_size": len(CLEANED),
            }
            values.update(kw)
            return SwapResult(**values)

    return Library()


# ------------------------------------------------------------- persist_swap


def test_a_swap_writes_one_row_and_updates_the_item(library) -> None:
    with session_scope() as session:
        ids = persist_swap(session, library.swap_result(), media_item_id=library.item_id)
        assert len(ids) == 1
    with session_scope() as session:
        row = session.get(Backup, ids[0])
        assert (row.state, row.size) == ("kept", len(ORIGINAL))
        assert row.sha1_prefix == fingerprint(library.backup)
        assert row.purge_after is not None
        item = session.get(MediaItem, library.item_id)
        assert item.status == "clean" and item.cleaned_at is not None
        assert item.size == len(CLEANED)


def test_each_sidecar_gets_its_own_row(library) -> None:
    """§5's scalar columns read as one row per job; several sharing a job id is what
    lets a restore put the subtitles back too."""
    result = library.swap_result(
        sidecars=[
            SidecarSwap(
                original_path=str(library.folder / "S01E01.srt"),
                backup_path=str(library.backups / "S01E01.srt"),
                staged_path="/work/red.srt",
                replacements=4,
            )
        ]
    )
    with session_scope() as session:
        ids = persist_swap(session, result, media_item_id=library.item_id, job_id="job-1")
    assert len(ids) == 2
    with session_scope() as session:
        rows = session.scalars(select(Backup).where(Backup.job_id == "job-1")).all()
        assert {Path(r.backup_path).suffix for r in rows} == {".mkv", ".srt"}


def test_an_mp4_swap_moves_the_item_to_the_new_path(library) -> None:
    result = library.swap_result(
        original_path=str(library.folder / "S01E01.mp4"),
        old_path=str(library.folder / "S01E01.mp4"),
    )
    with session_scope() as session:
        persist_swap(session, result, media_item_id=library.item_id)
        assert session.get(MediaItem, library.item_id).path.endswith(".mkv")


def test_an_earlier_backup_becomes_orphaned(library) -> None:
    """§13: an upgrade replaces the file, so the previous original belongs to
    nothing in the library any more and should be purged on the retention clock."""
    with session_scope() as session:
        first = persist_swap(session, library.swap_result(), media_item_id=library.item_id)
    with session_scope() as session:
        persist_swap(
            session,
            library.swap_result(backup_path=str(library.backups / "S01E01.vc-2.mkv")),
            media_item_id=library.item_id,
        )
    with session_scope() as session:
        assert session.get(Backup, first[0]).state == "orphaned"
        kept = session.scalars(select(Backup).where(Backup.state == "kept")).all()
        assert len(kept) == 1


def test_zero_retention_means_no_purge_deadline(library) -> None:
    with session_scope() as session:
        ids = persist_swap(
            session, library.swap_result(), media_item_id=library.item_id, retention_days=0
        )
        assert session.get(Backup, ids[0]).purge_after is None


# ------------------------------------------------------------- restore_item


def test_restore_puts_the_original_back_and_moves_the_clean_copy_aside(library) -> None:
    with session_scope() as session:
        persist_swap(session, library.swap_result(), media_item_id=library.item_id)
    with session_scope() as session:
        report = restore_item(session, library.item_id)

    assert library.final.read_bytes() == ORIGINAL
    assert report.displaced_path is not None
    assert Path(report.displaced_path).read_bytes() == CLEANED
    with session_scope() as session:
        item = session.get(MediaItem, library.item_id)
        assert item.status == "restored" and item.cleaned_at is None
        assert session.scalars(select(Backup)).one().state == "restored"


def test_restore_follows_a_rename(library) -> None:
    """The item was renamed after the swap, so `backups.original_path` is stale;
    restoring there would recreate the old name and leave two video files."""
    with session_scope() as session:
        persist_swap(session, library.swap_result(), media_item_id=library.item_id)

    renamed = library.folder / "S01E01 - New Title.mkv"
    library.final.rename(renamed)
    with session_scope() as session:
        session.get(MediaItem, library.item_id).path = str(renamed)

    with session_scope() as session:
        restore_item(session, library.item_id)

    assert renamed.read_bytes() == ORIGINAL
    assert not library.final.exists(), "the stale name is not recreated"


def test_restoring_an_mp4_leaves_one_video_file(library) -> None:
    mp4_backup = library.backups / "S01E01.mp4"
    mp4_backup.write_bytes(ORIGINAL)
    with session_scope() as session:
        persist_swap(
            session,
            library.swap_result(
                original_path=str(library.folder / "S01E01.mp4"),
                backup_path=str(mp4_backup),
            ),
            media_item_id=library.item_id,
        )
    with session_scope() as session:
        restore_item(session, library.item_id)

    videos = sorted(p.name for p in library.folder.iterdir() if p.suffix in (".mkv", ".mp4"))
    assert videos == ["S01E01.mp4"]


def test_restore_brings_the_sidecar_back_too(library) -> None:
    sidecar = library.folder / "S01E01.srt"
    sidecar.write_text("Oh ****.")
    sidecar_backup = library.backups / "S01E01.srt"
    sidecar_backup.write_text("Oh shit.")

    with session_scope() as session:
        persist_swap(
            session,
            library.swap_result(
                sidecars=[
                    SidecarSwap(
                        original_path=str(sidecar),
                        backup_path=str(sidecar_backup),
                        staged_path="/work/red.srt",
                    )
                ]
            ),
            media_item_id=library.item_id,
        )
    with session_scope() as session:
        report = restore_item(session, library.item_id)

    assert report.sidecars == 1
    assert sidecar.read_text() == "Oh shit."
    # The redacted copy is displaced to /backups, not left in the library folder as
    # `S01E01.srt.cleaned` for nobody to clean up (the same rule as the video).
    assert not list(library.folder.glob("*.cleaned"))
    assert (library.backups / "S01E01.srt.cleaned").read_text() == "Oh ****."


def test_restoring_without_a_backup_is_an_error(library) -> None:
    with session_scope() as session, pytest.raises(ValueError, match="no kept backup"):
        restore_item(session, library.item_id)


def test_a_failed_sidecar_restore_only_warns(library) -> None:
    with session_scope() as session:
        persist_swap(
            session,
            library.swap_result(
                sidecars=[
                    SidecarSwap(
                        original_path=str(library.folder / "S01E01.srt"),
                        backup_path=str(library.backups / "gone.srt"),
                        staged_path="/work/red.srt",
                    )
                ]
            ),
            media_item_id=library.item_id,
        )
    with session_scope() as session:
        report = restore_item(session, library.item_id)

    assert library.final.read_bytes() == ORIGINAL, "the video still came back"
    assert report.sidecars == 0
    assert any("gone.srt" in w for w in report.warnings)


# ---------------------------------------------------------- reconcile_backups


def test_an_untracked_backup_file_is_adopted_as_orphaned(library, tmp_path: Path) -> None:
    """The one window a rename cannot close: it committed, the SQLite commit did not."""
    stray = library.backups / "S02E05.mkv"
    stray.write_bytes(b"an original nobody recorded")

    with session_scope() as session:
        report = reconcile_backups(session, tmp_path / "backups")
    # The fixture's own (unrecorded) backup is adopted too, which is the point.
    assert report.adopted == 2
    with session_scope() as session:
        row = session.scalars(select(Backup).where(Backup.backup_path == str(stray))).one()
        assert row.state == "orphaned" and row.purge_after is not None


def test_a_row_whose_file_is_gone_is_marked_purged(library, tmp_path: Path) -> None:
    with session_scope() as session:
        persist_swap(session, library.swap_result(), media_item_id=library.item_id)
    library.backup.unlink()

    with session_scope() as session:
        report = reconcile_backups(session, tmp_path / "backups")
    assert report.purged == 1
    with session_scope() as session:
        assert session.scalars(select(Backup)).one().state == "purged"


def test_reconciling_is_idempotent(library, tmp_path: Path) -> None:
    (library.backups / "S02E05.mkv").write_bytes(b"stray")
    with session_scope() as session:
        first = reconcile_backups(session, tmp_path / "backups")
    with session_scope() as session:
        second = reconcile_backups(session, tmp_path / "backups")
    assert first.adopted > 0 and second.adopted == 0
    assert second.purged == 0


def test_the_ignore_marker_is_not_adopted(library, tmp_path: Path) -> None:
    """`/backups` defaults to a directory inside the media share, so it carries a
    `.ignore` file; adopting that as a lost original would be nonsense."""
    (tmp_path / "backups" / ".ignore").write_text("not media")
    with session_scope() as session:
        reconcile_backups(session, tmp_path / "backups")
    with session_scope() as session:
        paths = [r.backup_path for r in session.scalars(select(Backup))]
        assert not any(p.endswith(".ignore") for p in paths)
