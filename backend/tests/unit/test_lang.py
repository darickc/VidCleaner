"""ISO 639 tag handling, and the `und` distinction the render depends on."""

from __future__ import annotations

import pytest

from vidcleaner.pipeline import lang


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("und", None),
        ("UND", None),
        ("eng", "eng"),
        ("ENG", "eng"),
        ("en", "en"),
        ("pt-BR", "pt"),
        ("pt_BR", "pt"),
    ],
)
def test_effective_treats_und_as_no_language(tag, expected):
    assert lang.effective(tag) == expected


def test_normalize_tag_still_keeps_und():
    """`choose_source_audio` tells "tagged undetermined" from "untagged", and
    `probe.json` is persisted -- only the render's view collapses the two."""
    assert lang.normalize_tag("und") == lang.UNDETERMINED
    assert lang.normalize_tag(None) is None


@pytest.mark.parametrize(
    ("left", "right"),
    [("en", "eng"), ("eng", "en"), ("fra", "fre"), ("deu", "ger"), ("pt-BR", "por")],
)
def test_matches_spans_the_639_spellings(left, right):
    assert lang.matches(left, right)


def test_matches_rejects_different_languages():
    assert not lang.matches("eng", "fre")
