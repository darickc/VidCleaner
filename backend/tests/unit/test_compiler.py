"""Regex compilation, attribution, whitelisting and profile hashing (PLAN.md §7)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vidcleaner.db.constants import WORD_CATEGORIES
from vidcleaner.matching.compiler import (
    ALGO_VERSION,
    DEFAULT_CATEGORIES,
    ProfileSpec,
    WhitelistRule,
    build_matcher,
    compile_pattern,
    default_profile,
    mask_text,
    profile_hash,
    select_entries,
)
from vidcleaner.matching.wordlists import WordEntry, load_builtin_entries, load_never_match

ALL_CATEGORIES = frozenset(WORD_CATEGORIES)


def matcher(categories=DEFAULT_CATEGORIES, whitelist=(), **profile_kw):
    return build_matcher(
        load_builtin_entries(),
        ProfileSpec(categories=frozenset(categories), **profile_kw),
        whitelist,
    )


def hits(text, **kw):
    return [(m.canonical, m.raw) for m in matcher(**kw).finditer(text)]


def canonicals(text, **kw):
    return [c for c, _ in hits(text, **kw)]


# ------------------------------------------------------------- true positives


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("what the fuck", ["fuck"]),
        ("fucking hell", ["fuck"]),  # `hell` is mild, off by default
        ("motherfucking idiot", ["motherfucker"]),
        ("Bullshit.", ["bullshit"]),
        ("Bull. Shit.", ["shit"]),
        ("he's a badass", ["badass"]),
        ("sit on the ass", ["ass"]),
        ("son of a bitch", ["son of a bitch"]),
        ("Jesus Christ!", ["jesus christ"]),
        ("God damn it", ["god damn"]),
        ("Goddamn it.", ["goddamn"]),
        ("Christ, no.", ["christ"]),
        ("kick-ass", ["kick-ass"]),
        ("I can't feel jack shit", ["shit"]),
    ],
)
def test_true_positives(text, expected):
    assert canonicals(text) == expected


def test_compound_is_not_reported_as_its_parent():
    assert canonicals("bullshit") == ["bullshit"]
    assert canonicals("motherfucker") == ["motherfucker"]


def test_case_insensitive():
    for text in ("FUCK", "Fuck", "fUcK"):
        assert canonicals(text) == ["fuck"]


def test_longest_form_wins_and_raw_is_the_surface_text():
    assert hits("fucking") == [("fuck", "fucking")]


def test_overlapping_forms_are_not_double_counted():
    assert len(canonicals("motherfucking")) == 1


# --------------------------------------------------- the two §7 regex defects


def test_apostrophe_form_matches():
    """PLAN.md §7's `\\b...\\b` does not match `fuckin'`; the M1 media has `friggin'`."""
    assert hits("he was fuckin' tired") == [("fuck", "fuckin'")]


def test_curly_apostrophe_form_matches():
    assert hits("he was fuckin’ tired") == [("fuck", "fuckin’")]


def test_pattern_uses_no_word_boundary_escape():
    """Regression guard: reintroducing `\\b` silently breaks apostrophe forms."""
    pattern = matcher().pattern
    assert pattern is not None
    assert r"\b" not in pattern.pattern


def test_phrase_does_not_span_a_newline():
    """PLAN.md §7's `[\\s\\-']+` bridged a subtitle line break."""
    assert canonicals("oh my god\ndamn that hurt") == ["god"]


def test_phrase_separator_is_bounded():
    assert "god damn" not in canonicals("god          damn")


@pytest.mark.parametrize("text", ["god damn", "God  damn", "god-damn", "god'damn", "god\xa0damn"])
def test_phrase_separators_that_do_match(text):
    assert "god damn" in canonicals(text)


def test_possessive_matches_only_the_word():
    (hit,) = list(matcher().finditer("the fucker's dog"))
    assert hit.raw == "fucker"


def test_god_matches_inside_a_possessive():
    assert hits("for God's sake") == [("god", "God")]


def test_leftmost_phrase_beats_the_contained_word():
    assert len(canonicals("god damn it")) == 1


# ----------------------------------------------------------- profile selection


def test_profile_selects_only_enabled_categories():
    assert canonicals("damn", categories={"mild"}) == ["damn"]
    assert canonicals("damn", categories={"strong"}) == []


def test_default_profile_excludes_mild():
    assert "mild" not in DEFAULT_CATEGORIES
    assert canonicals("what the hell, damn, crap") == []


def test_default_profile_includes_the_other_four():
    assert set(DEFAULT_CATEGORIES) == {"strong", "slurs", "sexual", "religious"}


def test_disabled_entries_never_compile_in():
    """Bare `cock` ships disabled; `cocksucker` does not."""
    assert canonicals("the cock crowed", categories=ALL_CATEGORIES) == []
    assert canonicals("cocksucker", categories=ALL_CATEGORIES) == ["cocksucker"]


def test_extra_canonicals_bypass_the_category_filter():
    m = build_matcher(
        load_builtin_entries(),
        ProfileSpec(categories=frozenset({"strong"}), extra_canonicals=frozenset({"damn"})),
    )
    assert [x.canonical for x in m.finditer("damn")] == ["damn"]


def test_select_entries_skips_disabled():
    entries = (
        WordEntry("a", "strong", ("a",), enabled=True),
        WordEntry("b", "strong", ("b",), enabled=False),
    )
    kept = select_entries(entries, ProfileSpec(categories=frozenset({"strong"})))
    assert [e.canonical for e in kept] == ["a"]


def test_empty_profile_compiles_to_none_not_an_empty_alternation():
    """`re.compile(r"(?<!\\w)(?:)(?!\\w)")` would match at every position."""
    m = build_matcher(load_builtin_entries(), ProfileSpec(categories=frozenset()))
    assert m.pattern is None
    assert list(m.finditer("fuck")) == []


def test_compile_pattern_returns_none_for_no_entries():
    assert compile_pattern([]) is None


def test_default_profile_rejects_unknown_categories():
    with pytest.raises(ValueError, match="unknown categories"):
        default_profile({"rude"})


# ---------------------------------------------------------------- attribution


def test_every_entry_is_attributable():
    """`m.lastgroup` must resolve for a hit on every single enabled form."""
    m = matcher(categories=ALL_CATEGORIES)
    for entry in m.entries:
        probe = entry.forms[-1]  # shortest form
        found = [x.canonical for x in m.finditer(probe)]
        assert entry.canonical in found, f"{entry.canonical} via {probe!r} -> {found}"


def test_forms_of_returns_the_whole_inflection_table():
    forms = matcher().forms_of("fuck")
    assert {"fuck", "fucking", "fuckin", "fuckin'"} <= set(forms)


def test_forms_of_unknown_canonical_is_empty():
    assert matcher().forms_of("nope") == ()


# ------------------------------------------------------------------ whitelist


def test_global_whitelist_marks_hits_suppressed_but_still_matched():
    m = matcher(whitelist=[WhitelistRule("fuck")])
    (hit,) = list(m.finditer("what the fuck"))
    assert hit.canonical == "fuck"
    assert m.suppressed(hit) is True


def test_unwhitelisted_word_is_not_suppressed():
    m = matcher(whitelist=[WhitelistRule("shit")])
    (hit,) = list(m.finditer("what the fuck"))
    assert m.suppressed(hit) is False


def test_context_text_requires_the_context():
    rule = WhitelistRule("hell", context_text="hell of a")
    m = matcher(categories=ALL_CATEGORIES, whitelist=[rule])
    (a,) = list(m.finditer("what a hell of a day"))
    assert m.suppressed(a, "what a hell of a day") is True
    (b,) = list(m.finditer("go to hell"))
    assert m.suppressed(b, "go to hell") is False


def test_context_matching_is_folded_and_case_insensitive():
    m = matcher(whitelist=[WhitelistRule("spic", context_text="spic and span")])
    (hit,) = list(m.finditer("SPIC AND SPAN"))
    assert m.suppressed(hit, "SPIC AND SPAN") is True


def test_title_and_item_scopes_both_suppress():
    """Scopes are a union: a narrower scope can only add suppression."""
    m = matcher(
        whitelist=[
            WhitelistRule("fuck", scope="title", scope_id=7),
            WhitelistRule("shit", scope="item", scope_id=3),
        ]
    )
    assert all(m.suppressed(h) for h in m.finditer("fuck and shit"))


# --------------------------------------------------------------- profile hash


def _hash(**kw):
    profile = ProfileSpec(
        categories=frozenset(kw.pop("categories", DEFAULT_CATEGORIES)), **kw.pop("profile", {})
    )
    entries = select_entries(load_builtin_entries(), profile)
    return profile_hash(profile, entries, kw.pop("whitelist", ()), load_never_match().never_match)


def test_profile_hash_is_stable_across_runs():
    assert _hash() == _hash()


def test_profile_hash_is_prefixed_with_the_algorithm_version():
    assert _hash().startswith(f"v{ALGO_VERSION}:")


def test_profile_hash_changes_when_a_category_is_added():
    assert _hash() != _hash(categories=ALL_CATEGORIES)


@pytest.mark.parametrize("field", ["pad_pre_ms", "pad_post_ms", "merge_gap_ms"])
def test_profile_hash_changes_when_padding_changes(field):
    assert _hash() != _hash(profile={field: 999})


def test_profile_hash_changes_when_mute_censored_changes():
    assert _hash() != _hash(profile={"mute_censored_tokens": False})


def test_profile_hash_changes_when_a_whitelist_entry_is_added():
    assert _hash() != _hash(whitelist=(WhitelistRule("fuck"),))


def test_profile_hash_changes_for_an_item_scoped_whitelist():
    """Otherwise "add an item whitelist, Reprocess" short-circuits to already_clean."""
    a = _hash(whitelist=(WhitelistRule("fuck", scope="global"),))
    b = _hash(whitelist=(WhitelistRule("fuck", scope="item", scope_id=3),))
    assert a != b


def test_profile_hash_ignores_the_profile_name():
    entries = select_entries(load_builtin_entries(), ProfileSpec())
    a = profile_hash(ProfileSpec(name="A"), entries)
    b = profile_hash(ProfileSpec(name="B"), entries)
    assert a == b


def test_profile_hash_is_insensitive_to_whitelist_order():
    rules = (WhitelistRule("fuck"), WhitelistRule("shit"))
    assert _hash(whitelist=rules) == _hash(whitelist=tuple(reversed(rules)))


def test_matcher_exposes_the_effective_hash():
    assert matcher().profile_hash == _hash()


# -------------------------------------------------------------------- masking


@pytest.mark.parametrize(
    ("matched", "expected"),
    [
        ("shit", "****"),
        ("FUCKING", "*******"),
        ("God damn", "*** ****"),
        ("kick-ass", "****-***"),
        ("fuckin'", "******'"),
    ],
)
def test_mask_text_preserves_length_and_separators(matched, expected):
    assert mask_text(matched) == expected


def test_mask_char_is_configurable():
    assert mask_text("shit", "#") == "####"


def test_redaction_and_detection_agree_on_the_same_text():
    m = matcher()
    text = "Oh shit, you motherfucking bastard."
    out, cursor = [], 0
    for hit in m.finditer(text):
        out.append(text[cursor : hit.start])
        out.append(mask_text(hit.raw))
        cursor = hit.end
    out.append(text[cursor:])
    assert "".join(out) == "Oh ****, you ************* *******."


# ------------------------------------------------------------ censored indexes


def test_censor_index_covers_enabled_non_phrase_forms():
    m = matcher()
    assert ("f", 4) in m.by_letter_len
    assert any(c.canonical == "fuck" for c in m.by_letter_len[("f", 4)])


def test_censor_index_excludes_phrases():
    m = matcher()
    assert all(" " not in c.form for cands in m.by_letter.values() for c in cands)


def test_disabled_entries_are_absent_from_the_censor_index():
    m = matcher()
    assert all(c.canonical != "cock" for cands in m.by_letter.values() for c in cands)


def test_replace_helper_keeps_entries_immutable():
    entry = load_builtin_entries()[0]
    assert replace(entry, enabled=not entry.enabled).canonical == entry.canonical
