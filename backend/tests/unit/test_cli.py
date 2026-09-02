"""The ``vidcleaner`` command line entry point."""

from __future__ import annotations

import json

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
