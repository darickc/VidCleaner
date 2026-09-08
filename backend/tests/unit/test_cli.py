"""The ``vidcleaner`` command line entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidcleaner import __version__
from vidcleaner.cli import build_parser, main


def test_no_command_prints_help_and_fails(capsys):
    assert main([]) == 1
    assert "usage: vidcleaner" in capsys.readouterr().out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_parser_exposes_the_expected_commands():
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions and set(actions[0].choices) >= {"health", "words"}


def test_health_emits_json(migrated, capsys):
    assert main(["health"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == __version__
    assert payload["database"]["ok"] is True
    assert "ffmpeg" in payload


def test_words_check_passes_the_false_positive_gate(capsys):
    assert main(["words"]) == 0
    out = capsys.readouterr().out
    assert "corpus" in out and "FAILED" not in out


def test_words_check_defaults_to_the_shipped_profile(capsys):
    main(["words"])
    out = capsys.readouterr().out
    assert "['religious', 'sexual', 'slurs', 'strong']" in out


def test_words_check_honours_categories(capsys):
    assert main(["words", "check", "--categories", "strong,mild"]) == 0
    out = capsys.readouterr().out
    assert "['mild', 'strong']" in out


def test_words_check_rejects_an_unknown_category(capsys):
    assert main(["words", "check", "--categories", "rude"]) == 64
    assert "unknown categories" in capsys.readouterr().err


def test_words_list_shows_entries_with_lineage(capsys):
    assert main(["words", "list"]) == 0
    out = capsys.readouterr().out
    assert "motherfucker" in out
    assert "<- fuck" in out
    assert "- sexual     cock" in out, "disabled entries should be marked"


def test_words_list_covers_every_entry(capsys):
    from vidcleaner.matching.wordlists import load_builtin_entries

    main(["words", "list"])
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == len(load_builtin_entries())


# ------------------------------------------------------- clean / detect


def test_clean_and_detect_are_registered():
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert {"clean", "detect"} <= set(actions[0].choices)


def test_clean_accepts_the_documented_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "clean",
            "/media/x.mkv",
            "--out",
            "/tmp/out.mkv",
            "--dry-run",
            "--force",
            "--job-id",
            "j1",
            "--categories",
            "strong,mild",
            "--model",
            "tiny",
            "--no-db",
            "--json",
            "--quiet",
        ]
    )
    assert args.command == "clean"
    assert args.file == Path("/media/x.mkv")
    assert args.out == Path("/tmp/out.mkv")
    assert args.dry_run and args.force and args.no_db and args.as_json and args.quiet
    assert args.job_id == "j1"
    assert args.categories == "strong,mild"
    assert args.model == "tiny"


def test_detect_has_no_output_flags():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["detect", "/media/x.mkv", "--out", "/tmp/out.mkv"])
    with pytest.raises(SystemExit):
        parser.parse_args(["detect", "/media/x.mkv", "--dry-run"])


def test_detect_accepts_the_shared_flags():
    args = build_parser().parse_args(
        ["detect", "/media/x.mkv", "--categories", "strong", "--no-db"]
    )
    assert args.command == "detect" and args.no_db


def test_a_missing_file_is_reported_cleanly(capsys, tmp_path):
    assert main(["detect", str(tmp_path / "nope.mkv"), "--no-db"]) == 66
    assert "not a file" in capsys.readouterr().err


def test_clean_requires_a_file():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["clean"])


def test_the_transcript_and_detections_flags_are_paths():
    args = build_parser().parse_args(
        ["clean", "/media/x.mkv", "--transcript", "/t.json", "--detections", "/d.json"]
    )
    assert args.transcript == Path("/t.json")
    assert args.detections == Path("/d.json")


def test_titles_enable_defers_a_series_existing_episodes(migrated, capsys):
    """The CLI and the UI must not disagree about what enabling means (§2, M7)."""
    from tests.support.library import make_series
    from vidcleaner.db.models import MediaItem, Title
    from vidcleaner.db.session import session_scope

    title_id, episodes = make_series(episodes=2, arr_id=42, enabled=False)
    assert main(["titles", "enable", "--arr-id", "42"]) == 0
    out = capsys.readouterr().out
    assert "Deferred   2 existing file(s)" in out
    with session_scope() as session:
        assert session.get(Title, title_id).backfill_from is not None
        assert all(session.get(MediaItem, e).skip_backfill for e in episodes)


def test_titles_enable_all_keeps_the_old_behaviour(migrated, capsys):
    from tests.support.library import make_series
    from vidcleaner.db.models import MediaItem, Title
    from vidcleaner.db.session import session_scope

    title_id, episodes = make_series(episodes=2, arr_id=43, enabled=False)
    assert main(["titles", "enable", "--arr-id", "43", "--all"]) == 0
    with session_scope() as session:
        assert session.get(Title, title_id).backfill_from is None
        assert not any(session.get(MediaItem, e).skip_backfill for e in episodes)
