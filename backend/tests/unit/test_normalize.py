"""Token normalization and censored-token detection (PLAN.md §7, §12)."""

from __future__ import annotations

import pytest

from vidcleaner.matching.normalize import (
    detect_censored,
    fold,
    join_tokens,
    normalize,
    span_to_tokens,
    strip_wrappers,
    tokenize,
)

NEVER = frozenset(
    {"xray", "email", "tshirt", "alist", "uturn", "bside", "dday", "kpop", "coop", "scifi", "wifi"}
)


# --------------------------------------------------------------------------- levels


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Shit!"', "Shit"),
        ("(fuck),", "fuck"),
        ("shit.", "shit"),
        ("...hell...", "hell"),
        ("¿Qué?", "Qué"),
        ("f***", "f***"),
        ("s___", "s___"),
        ("f--k", "f--k"),
    ],
)
def test_outer_punctuation_is_stripped_but_mask_chars_survive(raw, expected):
    assert strip_wrappers(raw) == expected


def test_norm_keeps_internal_hyphen_and_apostrophe():
    assert normalize("Fuckin'") == "fuckin'"
    assert normalize("Half-Breed") == "half-breed"


def test_norm_folds_curly_apostrophes_and_dashes():
    assert normalize("fuckin’") == "fuckin'"
    assert normalize("half—breed") == "half-breed"


def test_fold_drops_apostrophes_and_hyphens():
    assert fold("fuckin'") == "fuckin"
    assert fold("half-breed") == "halfbreed"
    assert fold("God Damn") == "god damn"


def test_normalize_collapses_whitespace():
    assert normalize("  son   of \t a  bitch ") == "son of a bitch"


# --------------------------------------------------------------------- censored


@pytest.mark.parametrize("core", ["f***", "s___", "sh*t", "f**k", "b*tch", "a$$", "f#ck", "f@ck"])
def test_masked_tokens_are_detected(core):
    info = detect_censored(core, NEVER)
    assert info is not None and info.style == "masked"


@pytest.mark.parametrize("core", ["f--k", "s---", "b----"])
def test_double_dash_tokens_are_masked_style(core):
    info = detect_censored(core, NEVER)
    assert info is not None and info.style == "masked"


@pytest.mark.parametrize("core", ["f-ing", "f-in"])
def test_hyphen_tokens_are_detected(core):
    info = detect_censored(core, NEVER)
    assert info is not None and info.style == "hyphen"
    assert info.first_letter == "f"
    assert info.suffix in {"ing", "in"}


@pytest.mark.parametrize(
    "core",
    [
        "x-ray",
        "e-mail",
        "t-shirt",
        "a-list",
        "u-turn",
        "b-side",
        "d-day",
        "k-pop",
        "co-op",
        "wi-fi",
    ],
)
def test_never_match_hyphen_tokens_are_not_censored(core):
    """The single reason never_match.yaml is load-bearing at runtime."""
    assert detect_censored(core, NEVER) is None


@pytest.mark.parametrize(
    "core",
    ["hello", "well-known", "mother-in-law", "twenty-one", "shit", "re-elect", "self-aware"],
)
def test_ordinary_words_are_not_censored(core):
    assert detect_censored(core, NEVER) is None


@pytest.mark.parametrize("core", ["That...", "I...", "Wait...", "..."])
def test_ellipsis_is_not_treated_as_a_mask(core):
    """`.` is deliberately not a mask char: subtitle ellipsis is everywhere."""
    assert detect_censored(strip_wrappers(core), NEVER) is None


def test_revealed_letters_are_recorded():
    info = detect_censored("sh*t", NEVER)
    assert info is not None
    assert info.revealed == ((0, "s"), (1, "h"), (3, "t"))


def test_overlong_token_is_not_censored():
    assert detect_censored("a" + "*" * 30, NEVER) is None


# ------------------------------------------------------------------- tokenize


def test_tokenize_classifies_kinds():
    assert tokenize("Shit!", never_match=NEVER).kind == "word"
    assert tokenize("f***", never_match=NEVER).kind == "censored"
    assert tokenize("--", never_match=NEVER).kind == "empty"
    assert tokenize("?", never_match=NEVER).kind == "empty"


def test_tokenize_carries_timing():
    tok = tokenize("fucking", index=3, start_s=1.5, end_s=1.9, prob=0.87, never_match=NEVER)
    assert (tok.index, tok.start_s, tok.end_s, tok.prob) == (3, 1.5, 1.9, 0.87)
    assert tok.norm == "fucking" and tok.fold == "fucking"


def test_empty_token_has_blank_norm():
    assert tokenize("...", never_match=NEVER).norm == ""


# ---------------------------------------------------------------- offset maps


def _toks(words):
    return [tokenize(w, index=i, never_match=NEVER) for i, w in enumerate(words)]


def test_join_round_trips_every_token_slice():
    toks = _toks(["You", "fucking", "idiot", ".", "Shit"])
    j = join_tokens(toks)
    for i, tok in enumerate(toks):
        assert j.text[j.starts[i] : j.ends[i]] == tok.norm


def test_join_uses_single_spaces_and_no_newline():
    j = join_tokens(_toks(["oh", "my", "god", "damn"]))
    assert j.text == "oh my god damn"
    assert "\n" not in j.text


def test_punctuation_only_token_does_not_shift_indices():
    toks = _toks(["god", "...", "damn"])
    j = join_tokens(toks)
    assert j.text == "god damn"
    assert j.starts[1] == j.ends[1]
    assert j.text[j.starts[2] : j.ends[2]] == "damn"


def test_span_to_tokens_single_word():
    j = join_tokens(_toks(["you", "fucking", "idiot"]))
    start = j.text.index("fucking")
    assert span_to_tokens(j, start, start + len("fucking")) == (1, 1)


def test_span_to_tokens_phrase_spans_two_tokens():
    j = join_tokens(_toks(["oh", "god", "damn", "it"]))
    start = j.text.index("god damn")
    assert span_to_tokens(j, start, start + len("god damn")) == (1, 2)


def test_span_to_tokens_first_and_last():
    j = join_tokens(_toks(["shit", "happens", "always"]))
    assert span_to_tokens(j, 0, 4) == (0, 0)
    last = j.text.rindex("always")
    assert span_to_tokens(j, last, len(j.text)) == (2, 2)


def test_span_starting_on_a_separator_advances():
    j = join_tokens(_toks(["a", "shit"]))
    sep = j.text.index(" ")
    assert span_to_tokens(j, sep, sep + 5) == (1, 1)


def test_span_to_tokens_rejects_empty_sequence():
    with pytest.raises(ValueError, match="empty token sequence"):
        span_to_tokens(join_tokens([]), 0, 1)
