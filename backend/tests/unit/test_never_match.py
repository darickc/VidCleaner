"""The build gate on word-list precision (PLAN.md §7, §12).

This asserts what PLAN.md §7 only claims: that `(?<!\\w)(?:...)(?!\\w)` boundaries
plus explicit `compounds` entries reject every classic substring false positive
WITHOUT any help from never_match.yaml. It runs against the widest possible
matcher -- all five categories, every entry force-enabled -- so it also fails
the day someone adds a bare `ass` form or a generic plural rule.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

import pytest

from vidcleaner.db.constants import WORD_CATEGORIES
from vidcleaner.matching.compiler import ProfileSpec, build_matcher
from vidcleaner.matching.normalize import detect_censored
from vidcleaner.matching.wordlists import load_builtin_entries, load_never_match

ALL_CATEGORIES = frozenset(WORD_CATEGORIES)
CORPUS = load_never_match().regression_corpus


@lru_cache(maxsize=1)
def widest_matcher():
    forced = tuple(replace(e, enabled=True) for e in load_builtin_entries())
    return build_matcher(forced, ProfileSpec(categories=ALL_CATEGORIES))


def test_the_corpus_is_not_empty():
    assert len(CORPUS) >= 50


@pytest.mark.parametrize("word", CORPUS, ids=CORPUS)
def test_innocent_word_produces_no_match(word):
    hits = [m.canonical for m in widest_matcher().finditer(word)]
    assert hits == [], f"{word!r} matched {hits}"


@pytest.mark.parametrize("word", CORPUS, ids=CORPUS)
def test_innocent_word_produces_no_match_in_a_sentence(word):
    """Boundaries must hold mid-sentence, not just for a bare token."""
    hits = [m.canonical for m in widest_matcher().finditer(f"we saw the {word} there")]
    assert hits == [], f"{word!r} matched {hits}"


@pytest.mark.parametrize("token", sorted(load_never_match().never_match))
def test_never_match_tokens_are_never_classified_as_censored(token):
    assert detect_censored(token, load_never_match().never_match) is None
