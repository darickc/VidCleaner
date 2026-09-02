"""`vidcleaner clean` and `detect` end to end -- PLAN.md §11's M1 demo, as a test.

Uses `--transcript` so the whole flow runs without the STT stack: the point here
is the CLI, the artifacts, the report and the database bookkeeping, not
recognition quality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from vidcleaner.cli import main
from vidcleaner.db.models import Detection as DetectionRow
from vidcleaner.db.models import Job, MediaItem
from vidcleaner.db.session import session_scope
from vidcleaner.pipeline.artifacts import Transcript, TranscriptSegment, TranscriptWord

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def transcript_file(tmp_path) -> Path:
    """A transcript matching `tests/fixtures/marked.srt`, with real timings."""
    words = [
        ("Oh", 0.55, 0.65),
        ("shit", 0.70, 0.95),
        ("that", 1.00, 1.20),
        ("hurt", 1.25, 1.45),
        ("You", 2.05, 2.15),
        ("fucking", 2.20, 2.65),
        ("idiot", 2.70, 2.95),
        ("Nothing", 3.55, 3.80),
        ("to", 3.85, 3.90),
        ("see", 3.95, 4.10),
        ("in", 4.15, 4.20),
        ("Scunthorpe", 4.25, 4.50),
        ("God", 5.05, 5.25),
        ("damn", 5.30, 5.60),
        ("it", 5.65, 5.80),
        ("He", 6.55, 6.65),
        ("was", 6.70, 6.80),
        ("friggin", 6.85, 7.10),
        ("tired", 7.15, 7.45),
        ("Bullshit", 8.05, 8.45),
        ("Bull", 8.50, 8.70),
        ("Shit", 8.75, 8.95),
    ]
    transcript = Transcript(
        mode="windowed",
        model="fixture",
        language="en",
        segments=[
            TranscriptSegment(
                start=words[0][1],
                end=words[-1][2],
                text=" ".join(w for w, _, _ in words),
                words=[
                    TranscriptWord(word=w, start=s, end=e, probability=0.9, aligned=True)
                    for w, s, e in words
                ],
            )
        ],
    )
    path = tmp_path / "transcript.json"
    transcript.write(path)
    return path


def run(*args: str) -> int:
    return main(list(args))


# --------------------------------------------------------------- detect


def test_detect_prints_the_word_counts(migrated, sample_mkv, transcript_file, tmp_path, capsys):
    code = run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
        "--no-db",
    )
    assert code == 0


def test_detect_report_lists_every_word(migrated, sample_mkv, transcript_file, tmp_path, capsys):
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "work"),
        "--no-db",
    )
    out = capsys.readouterr().out
    assert "Detections" in out
    assert "WORD" in out and "CATEGORY" in out
    for word in ("shit", "fuck", "god damn", "bullshit"):
        assert word in out, f"{word} missing from the report"
    assert "TOTAL" in out
    assert "Dry run" in out


def test_detect_does_not_render(migrated, sample_mkv, transcript_file, tmp_path):
    work = tmp_path / "work"
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(work),
        "--quiet",
        "--no-db",
    )
    assert not list(work.rglob("out.mkv"))
    assert list(work.rglob("detections.json"))


def test_json_output_is_machine_readable(migrated, sample_mkv, transcript_file, tmp_path, capsys):
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "work"),
        "--json",
        "--no-db",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "done"
    assert payload["dry_run"] is True
    assert payload["render"] is None
    assert payload["probe"]["duration"] > 0
    assert payload["detections"]["counts"]
    assert payload["profile_hash"].startswith("v1:")


def test_categories_override_the_profile(migrated, sample_mkv, transcript_file, tmp_path, capsys):
    """`mild` is off by default, so `friggin'` is only found when asked for."""
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "w1"),
        "--json",
        "--no-db",
    )
    default = json.loads(capsys.readouterr().out)

    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "w2"),
        "--categories",
        "strong,mild",
        "--json",
        "--no-db",
    )
    widened = json.loads(capsys.readouterr().out)

    words = {c["word_canonical"] for c in widened["detections"]["counts"]}
    assert "frigging" in words
    assert default["profile_hash"] != widened["profile_hash"]


def test_never_match_words_stay_out_of_the_report(
    migrated, sample_mkv, transcript_file, tmp_path, capsys
):
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "work"),
        "--json",
        "--no-db",
    )
    payload = json.loads(capsys.readouterr().out)
    words = {c["word_canonical"] for c in payload["detections"]["counts"]}
    assert "Scunthorpe" not in words and "scunthorpe" not in words


# ---------------------------------------------------------------- clean


def test_clean_writes_a_playable_mkv(migrated, sample_mkv, transcript_file, tmp_path):
    out = tmp_path / "clean.mkv"
    code = run(
        "clean",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--out",
        str(out),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
        "--no-db",
    )
    assert code == 0
    assert out.is_file() and out.stat().st_size > 0


def test_clean_output_has_the_expected_track_layout(
    migrated, sample_mkv, transcript_file, tmp_path
):
    from vidcleaner.pipeline.ffmpeg import FFmpegRunner

    out = tmp_path / "clean.mkv"
    run(
        "clean",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--out",
        str(out),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
        "--no-db",
    )
    data = FFmpegRunner().probe(out)
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    assert audio[0]["tags"]["title"] == "Clean"
    assert audio[0]["disposition"]["default"] == 1
    assert audio[1]["tags"]["title"] == "Original"
    assert audio[1]["disposition"]["default"] == 0


def test_clean_reports_render_and_verify(migrated, sample_mkv, transcript_file, tmp_path, capsys):
    run(
        "clean",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--out",
        str(tmp_path / "clean.mkv"),
        "--work-dir",
        str(tmp_path / "work"),
        "--no-db",
    )
    out = capsys.readouterr().out
    assert "Render" in out
    assert "Verify     OK" in out
    assert "Wrote" in out


def test_a_second_run_resumes_from_the_markers(
    migrated, sample_mkv, transcript_file, tmp_path, capsys
):
    """The deterministic job id is what makes this work."""
    work = tmp_path / "work"
    args = (
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(work),
        "--quiet",
        "--no-db",
    )
    run(*args)
    first = {p.name for p in work.rglob("*.done")}
    run(*args)
    assert first == {p.name for p in work.rglob("*.done")}
    assert first >= {"probe.done", "extract.done", "subtitles.done", "detect.done"}


def test_force_reruns_everything(migrated, sample_mkv, transcript_file, tmp_path):
    work = tmp_path / "work"
    args = [
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(work),
        "--quiet",
        "--no-db",
    ]
    run(*args)
    marker = next(work.rglob("probe.done"))
    before = marker.stat().st_mtime_ns
    run(*args, "--force")
    assert marker.stat().st_mtime_ns != before


def test_detections_flag_skips_straight_to_render(migrated, sample_mkv, transcript_file, tmp_path):
    """Also the mechanism behind M4's "reprocess after a whitelist edit"."""
    work = tmp_path / "work"
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(work),
        "--quiet",
        "--no-db",
    )
    detections = next(work.rglob("detections.json"))

    out = tmp_path / "reprocessed.mkv"
    code = run(
        "clean",
        str(sample_mkv),
        "--detections",
        str(detections),
        "--out",
        str(out),
        "--work-dir",
        str(tmp_path / "work2"),
        "--quiet",
        "--no-db",
    )
    assert code == 0 and out.is_file()


def test_clean_is_idempotent_on_its_own_output(
    migrated, sample_mkv, transcript_file, tmp_path, capsys
):
    out = tmp_path / "clean.mkv"
    run(
        "clean",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--out",
        str(out),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
        "--no-db",
    )
    capsys.readouterr()

    run(
        "detect",
        str(out),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "work2"),
        "--no-db",
    )
    assert "Already clean" in capsys.readouterr().out


# ------------------------------------------------------------- database


def test_a_run_is_recorded_in_the_database(migrated, sample_mkv, transcript_file, tmp_path):
    out = tmp_path / "clean.mkv"
    run(
        "clean",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--out",
        str(out),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
    )
    with session_scope() as session:
        job = session.scalars(select(Job)).one()
        item = session.get(MediaItem, job.media_item_id)
        rows = session.scalars(select(DetectionRow)).all()

        assert job.state == "done"
        assert job.trigger == "manual"
        assert item.status == "clean"
        assert item.last_job_id == job.id
        assert rows and all(r.job_id == job.id for r in rows)
        assert {r.word_canonical for r in rows} >= {"shit", "fuck", "god damn", "bullshit"}


def test_no_db_writes_nothing(migrated, sample_mkv, transcript_file, tmp_path):
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
        "--no-db",
    )
    with session_scope() as session:
        assert session.scalars(select(Job)).all() == []
        assert session.scalars(select(MediaItem)).all() == []


def test_a_dry_run_records_the_item_as_pending(migrated, sample_mkv, transcript_file, tmp_path):
    run(
        "detect",
        str(sample_mkv),
        "--transcript",
        str(transcript_file),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
    )
    with session_scope() as session:
        job = session.scalars(select(Job)).one()
        assert job.dry_run is True
        assert session.get(MediaItem, job.media_item_id).status == "pending"


# ------------------------------------------------- in-place swap (M3 step 6)


@pytest.fixture
def in_library(sample_mkv, tmp_path, monkeypatch):
    """Move the fixture into a `/media`-shaped tree with its own `/backups`."""
    from vidcleaner.config import get_settings
    from vidcleaner.db.session import reset_engine_cache

    media = tmp_path / "media"
    folder = media / "tv" / "Show"
    folder.mkdir(parents=True)
    target = folder / "S01E01.mkv"
    sample_mkv.rename(target)

    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    monkeypatch.setenv("VIDCLEANER_MEDIA_DIR", str(media))
    monkeypatch.setenv("VIDCLEANER_BACKUPS_DIR", str(backups))
    get_settings.cache_clear()
    reset_engine_cache()
    yield target
    get_settings.cache_clear()
    reset_engine_cache()


def clean_in_place(source: Path, transcript_file: Path, work: Path) -> int:
    return run(
        "clean",
        str(source),
        "--transcript",
        str(transcript_file),
        "--in-place",
        "--work-dir",
        str(work),
        "--quiet",
    )


def test_in_place_replaces_the_library_file_and_keeps_the_original(
    migrated, in_library, transcript_file, tmp_path
):
    original = in_library.read_bytes()
    assert clean_in_place(in_library, transcript_file, tmp_path / "work") == 0

    assert in_library.is_file()
    assert in_library.read_bytes() != original, "the library file is the cleaned one"

    backup = tmp_path / "backups" / "tv" / "Show" / "S01E01.mkv"
    assert backup.read_bytes() == original, "the backup is byte-identical to the original"
    # §3: exactly one video file, or an arr may adopt the wrong one.
    assert sorted(p.name for p in in_library.parent.iterdir()) == ["S01E01.mkv"]


def test_the_swap_is_recorded_and_reversible(migrated, in_library, transcript_file, tmp_path):
    original = in_library.read_bytes()
    assert clean_in_place(in_library, transcript_file, tmp_path / "work") == 0

    with session_scope() as session:
        from vidcleaner.db.models import Backup

        row = session.scalars(select(Backup)).one()
        assert row.state == "kept"
        item = session.get(MediaItem, row.media_item_id)
        assert item.status == "clean" and item.path == str(in_library)

    assert run("restore", "--path", str(in_library)) == 0
    assert in_library.read_bytes() == original, "restore is byte-identical"
    assert sorted(p.name for p in in_library.parent.iterdir()) == ["S01E01.mkv"]
    # The cleaned copy went to /backups rather than staying in the media share.
    assert (tmp_path / "backups" / "tv" / "Show" / "S01E01.mkv.cleaned").is_file()

    with session_scope() as session:
        from vidcleaner.db.models import Backup

        assert session.scalars(select(Backup)).one().state == "restored"
        assert session.get(MediaItem, row.media_item_id).status == "restored"


def test_a_second_in_place_run_reports_already_clean(
    migrated, in_library, transcript_file, tmp_path, capsys
):
    """§4's idempotency loop, now across a real swap: the tag is in the library file."""
    assert clean_in_place(in_library, transcript_file, tmp_path / "work") == 0
    capsys.readouterr()

    assert (
        run(
            "clean",
            str(in_library),
            "--transcript",
            str(transcript_file),
            "--in-place",
            "--work-dir",
            str(tmp_path / "work2"),
        )
        == 0
    )
    assert "Already clean" in capsys.readouterr().out


def test_a_sidecar_is_swapped_alongside_the_video(migrated, in_library, transcript_file, tmp_path):
    sidecar = in_library.with_suffix(".srt")
    sidecar.write_text((FIXTURES / "marked.srt").read_text())
    original_subs = sidecar.read_text()

    assert clean_in_place(in_library, transcript_file, tmp_path / "work") == 0

    assert "****" in sidecar.read_text(), "the library subtitle is redacted"
    backup = tmp_path / "backups" / "tv" / "Show" / "S01E01.srt"
    assert backup.read_text() == original_subs

    assert run("restore", "--path", str(in_library)) == 0
    assert sidecar.read_text() == original_subs, "restore brings the subtitle back too"


def test_in_place_refuses_to_combine_with_dry_run(migrated, in_library, transcript_file, tmp_path):
    assert (
        run(
            "clean",
            str(in_library),
            "--transcript",
            str(transcript_file),
            "--in-place",
            "--dry-run",
            "--quiet",
        )
        == 64
    )


def test_without_in_place_the_library_is_untouched(migrated, in_library, transcript_file, tmp_path):
    original = in_library.read_bytes()
    run(
        "clean",
        str(in_library),
        "--transcript",
        str(transcript_file),
        "--out",
        str(tmp_path / "clean.mkv"),
        "--work-dir",
        str(tmp_path / "work"),
        "--quiet",
    )
    assert in_library.read_bytes() == original
    assert not (tmp_path / "backups" / "tv").exists()
