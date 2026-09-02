"""What the swap decides before it moves anything (PLAN.md §6 step 8).

Pure planning and refusal. Every assertion here is about a case where the library
must be left exactly as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vidcleaner.pipeline import swap
from vidcleaner.pipeline.artifacts import RedactedSubtitle, RenderResult, SwapPlan
from vidcleaner.pipeline.stages import StageError, StaleSourceError
from vidcleaner.pipeline.swap import (
    STAGED_PREFIX,
    RealFs,
    backup_path_for,
    plan_swap,
    preflight,
)

VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}


class Spec:
    def __init__(self, job_id: str = "0123456789abcdef") -> None:
        self.job_id = job_id


def library(tmp_path: Path, name: str = "S01E01.mkv", *, size: int = 4096) -> tuple[Path, Path]:
    media = tmp_path / "media"
    (media / "tv" / "Show").mkdir(parents=True, exist_ok=True)
    source = media / "tv" / "Show" / name
    source.write_bytes(b"x" * size)
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    return source, backups


def probe_for(source: Path):
    from vidcleaner.pipeline.artifacts import CodecPlan, ProbeResult

    return ProbeResult(
        path=str(source),
        size=source.stat().st_size,
        mtime=source.stat().st_mtime,
        inode=source.stat().st_ino,
        clean_codec=CodecPlan(encoder="eac3", reason="test"),
    )


def render_for(out: Path, *, sidecars: tuple[RedactedSubtitle, ...] = ()) -> RenderResult:
    return RenderResult(
        out_path=str(out),
        size=out.stat().st_size if out.is_file() else 0,
        redacted=list(sidecars),
        tags={"VIDCLEANER_JOB": "0123456789abcdef"},
    )


def make_plan(tmp_path: Path, *, name: str = "S01E01.mkv", out_size: int = 5000, **kw) -> SwapPlan:
    source, backups = library(tmp_path, name)
    out = tmp_path / "work" / "out.mkv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"y" * out_size)
    return plan_swap(
        Spec(),
        probe_for(source),
        render_for(out, **kw),
        media_dir=tmp_path / "media",
        backups_dir=backups,
        fs=RealFs(),
    )


# -------------------------------------------------------------------- naming


def test_an_mkv_is_replaced_in_place(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    assert plan.final_path == plan.source_path
    assert plan.extension_changed is False


def test_an_mp4_becomes_an_mkv_and_the_old_name_is_remembered(tmp_path: Path) -> None:
    plan = make_plan(tmp_path, name="S01E01.mp4")
    assert plan.final_path.endswith("S01E01.mkv")
    assert plan.source_path.endswith("S01E01.mp4")
    assert plan.extension_changed is True


def test_the_staged_name_is_not_a_video_file(tmp_path: Path) -> None:
    """§3: a sibling video file can be adopted by Sonarr as *the* file, and a disk
    scan enumerates by extension."""
    plan = make_plan(tmp_path)
    staged = Path(plan.staged_path)
    assert staged.suffix.lower() not in VIDEO_SUFFIXES
    assert staged.name.startswith(STAGED_PREFIX)
    # Staged beside its destination, so installing it is a rename, not a copy.
    assert staged.parent == Path(plan.final_path).parent


def test_the_backup_mirrors_the_library_layout(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    assert Path(plan.backup_path) == tmp_path / "backups" / "tv" / "Show" / "S01E01.mkv"


def test_a_file_outside_the_media_dir_still_gets_a_backup_path(tmp_path: Path) -> None:
    source = tmp_path / "elsewhere" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x")
    backup = backup_path_for(
        source,
        media_dir=tmp_path / "media",
        backups_dir=tmp_path / "backups",
        job_id="job",
        fs=RealFs(),
    )
    assert (tmp_path / "backups") in backup.parents
    assert backup.name == "movie.mkv"


def test_a_second_clean_does_not_overwrite_the_earlier_original(tmp_path: Path) -> None:
    source, backups = library(tmp_path)
    first = backup_path_for(
        source, media_dir=tmp_path / "media", backups_dir=backups, job_id="aaaaaaaa", fs=RealFs()
    )
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"the original")

    second = backup_path_for(
        source, media_dir=tmp_path / "media", backups_dir=backups, job_id="bbbbbbbb", fs=RealFs()
    )
    assert second != first
    assert "vc-bbbbbbbb" in second.name
    assert second.suffix == ".mkv", "the audit pass has to be able to probe a backup"


def test_the_plan_records_the_permissions_to_copy(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    assert plan.mode is not None and plan.mode > 0
    assert plan.source_inode is not None
    assert plan.source_size == 4096
    assert plan.out_size == 5000


def test_redacted_sidecars_become_part_of_the_plan(tmp_path: Path) -> None:
    source, backups = library(tmp_path)
    sidecar = source.with_suffix(".srt")
    sidecar.write_text("original")
    staged = tmp_path / "work" / "redacted" / "side_0.srt"
    staged.parent.mkdir(parents=True)
    staged.write_text("masked")
    out = tmp_path / "work" / "out.mkv"
    out.write_bytes(b"y" * 5000)

    plan = plan_swap(
        Spec(),
        probe_for(source),
        render_for(
            out,
            sidecars=(
                RedactedSubtitle(
                    sidecar_source=str(sidecar), output_path=str(staged), replacements=3
                ),
            ),
        ),
        media_dir=tmp_path / "media",
        backups_dir=backups,
        fs=RealFs(),
    )
    assert len(plan.sidecars) == 1
    entry = plan.sidecars[0]
    assert entry.original_path == str(sidecar)
    assert entry.staged_path == str(staged)
    assert entry.replacements == 3
    # Backed up beside its video, mirroring the library layout.
    assert Path(entry.backup_path) == backups / "tv" / "Show" / "S01E01.srt"


def test_an_embedded_redaction_is_not_a_sidecar(tmp_path: Path) -> None:
    plan = make_plan(
        tmp_path,
        sidecars=(RedactedSubtitle(stream_typed_index=0, output_path="/w/red_0.srt"),),
    )
    assert plan.sidecars == []


# ----------------------------------------------------------------- preflight


def test_preflight_accepts_a_sane_plan(tmp_path: Path) -> None:
    preflight(make_plan(tmp_path), fs=RealFs())


def test_a_vanished_source_is_stale_not_failed(tmp_path: Path) -> None:
    """§6's "path vanished" is a recovery path: re-resolve and requeue once."""
    plan = make_plan(tmp_path)
    Path(plan.source_path).unlink()
    with pytest.raises(StaleSourceError):
        preflight(plan, fs=RealFs())


def test_a_source_that_changed_size_is_stale(tmp_path: Path) -> None:
    """Minutes of rendering separate `verify`'s check from this one."""
    plan = make_plan(tmp_path)
    Path(plan.source_path).write_bytes(b"z" * 99)
    with pytest.raises(StaleSourceError, match="size"):
        preflight(plan, fs=RealFs())


def test_a_replaced_source_is_stale(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    with pytest.raises(StaleSourceError, match="inode"):
        preflight(plan.model_copy(update={"source_inode": 1}), fs=RealFs())


def test_an_existing_backup_path_is_refused(tmp_path: Path) -> None:
    plan = make_plan(tmp_path)
    backup = Path(plan.backup_path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(b"someone else")
    with pytest.raises(StageError, match="already exists"):
        preflight(plan, fs=RealFs())


def test_an_existing_mkv_beside_an_mp4_is_refused(tmp_path: Path) -> None:
    """We know nothing about that file; clobbering it is not ours to do."""
    plan = make_plan(tmp_path, name="S01E01.mp4")
    Path(plan.final_path).write_bytes(b"a different file")
    with pytest.raises(StageError, match="refusing to overwrite"):
        preflight(plan, fs=RealFs())


def test_a_cross_device_backup_is_refused_by_default(tmp_path: Path) -> None:
    """It would mean copy + verify + unlink the original, and CLAUDE.md says the
    swap never unlinks a library file. §10 assumed one filesystem; this enforces it."""
    plan = make_plan(tmp_path).model_copy(update={"backup_via": "copy"})
    with pytest.raises(StageError, match="different filesystem"):
        preflight(plan, fs=RealFs())
    preflight(plan, allow_cross_device_backup=True, fs=RealFs())


def test_a_backup_dir_inside_the_library_folder_is_refused(tmp_path: Path) -> None:
    """It would recreate §3's two-video-files hazard with real extensions."""
    plan = make_plan(tmp_path)
    library_folder = str(Path(plan.source_path).parent.parent)
    inside = plan.model_copy(
        update={"backup_path": str(Path(plan.source_path).parent / "backups" / "S01E01.mkv")}
    )
    with pytest.raises(StageError, match="inside the library folder"):
        preflight(inside, fs=RealFs(), arr_paths=(library_folder,))


def test_staging_by_copy_needs_room_on_the_library_filesystem(tmp_path: Path) -> None:
    plan = make_plan(tmp_path).model_copy(update={"stage_via": "copy", "out_size": 1 << 60})
    with pytest.raises(StageError, match="free"):
        preflight(plan, fs=RealFs())


def test_the_ignore_marker_keeps_jellyfin_out_of_the_backups(tmp_path: Path) -> None:
    """`/backups` defaults to a directory inside the media share (§10)."""
    root = tmp_path / "backups"
    swap.write_ignore_marker(root)
    assert (root / swap.IGNORE_MARKER).is_file()
    swap.write_ignore_marker(root)  # idempotent
