"""Built-in word list loading and validation (PLAN.md §7, §12)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from vidcleaner.db.constants import WORD_CATEGORIES
from vidcleaner.matching.normalize import fold
from vidcleaner.matching.wordlists import (
    WordListError,
    entries_by_category,
    load_builtin_entries,
    load_never_match,
)

STRONG = "version: 1\ncategory: strong\nentries:\n"

NEVER_MATCH_STUB = """
version: 1
never_match: [x-ray]
regression_corpus: [hello]
default_whitelist: []
"""


def write_data_dir(tmp_path: Path, body: str, *, name: str = "strong") -> Path:
    (tmp_path / "wordlists").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wordlists" / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    (tmp_path / "never_match.yaml").write_text(NEVER_MATCH_STUB, encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------ the shipped lists


def test_every_builtin_file_loads_and_validates():
    entries = load_builtin_entries()
    assert entries, "no built-in entries loaded"
    assert all(e.is_builtin for e in entries)


def test_all_five_categories_are_present_and_populated():
    by_cat = entries_by_category(load_builtin_entries())
    assert set(by_cat) == set(WORD_CATEGORIES)
    for category, items in by_cat.items():
        assert len(items) >= 5, f"{category} looks truncated: {len(items)} entries"


def test_shipped_lists_are_substantial():
    """Guards against a file being truncated or silently emptied."""
    entries = load_builtin_entries()
    assert len(entries) >= 150
    assert sum(len(e.forms) for e in entries) >= 400


def test_canonicals_are_unique_across_all_files():
    entries = load_builtin_entries()
    canonicals = [e.canonical for e in entries]
    assert len(canonicals) == len(set(canonicals))


def test_no_form_appears_in_two_entries():
    """The invariant that makes match -> entry attribution unambiguous."""
    owner: dict[str, str] = {}
    for entry in load_builtin_entries():
        for form in entry.forms:
            assert owner.setdefault(form, entry.canonical) == entry.canonical


def test_canonical_is_always_in_its_own_forms():
    for entry in load_builtin_entries():
        assert entry.canonical in entry.forms


def test_phrase_flag_matches_whitespace_in_canonical():
    for entry in load_builtin_entries():
        assert entry.is_phrase == (" " in entry.canonical)


def test_non_phrase_entries_have_no_multiword_forms():
    for entry in load_builtin_entries():
        if not entry.is_phrase:
            assert not any(" " in f for f in entry.forms), entry.canonical


def test_phrase_entries_have_a_multiword_form():
    for entry in load_builtin_entries():
        if entry.is_phrase:
            assert any(" " in f for f in entry.forms), entry.canonical


def test_focus_words_are_part_of_the_canonical():
    for entry in load_builtin_entries():
        for word in entry.focus:
            assert word in entry.canonical.split()


def test_no_form_collides_with_never_match():
    blocked = load_never_match().never_match
    for entry in load_builtin_entries():
        for form in entry.forms:
            assert fold(form) not in blocked, f"{entry.canonical}/{form}"


def test_disabled_entries_are_all_documented():
    for entry in load_builtin_entries():
        if not entry.enabled:
            assert entry.note, f"{entry.canonical} is disabled without a note"


def test_compounds_become_top_level_entries_with_lineage():
    entries = {e.canonical: e for e in load_builtin_entries()}
    mf = entries["motherfucker"]
    assert mf.category == "strong"
    assert mf.parent == "fuck"
    assert "motherfucking" in mf.forms
    # ...and the parent does not absorb the compound's forms
    assert "motherfucking" not in entries["fuck"].forms


def test_forms_are_sorted_longest_first():
    for entry in load_builtin_entries():
        lengths = [len(f) for f in entry.forms]
        assert lengths == sorted(lengths, reverse=True)


def test_default_whitelist_seeds_reference_real_canonicals():
    canonicals = {e.canonical for e in load_builtin_entries()}
    for seed in load_never_match().default_whitelist:
        assert seed.canonical in canonicals


# ------------------------------------------------------------------ validation


def test_rejects_duplicate_canonical_across_files(tmp_path):
    write_data_dir(tmp_path, STRONG + "  - {canonical: fuck, forms: [fuck]}\n")
    (tmp_path / "wordlists" / "mild.yaml").write_text(
        "version: 1\ncategory: mild\nentries:\n  - {canonical: fuck, forms: [fucks]}\n",
        encoding="utf-8",
    )
    with pytest.raises(WordListError, match="appears in both"):
        load_builtin_entries(tmp_path)


def test_rejects_form_claimed_by_two_entries(tmp_path):
    write_data_dir(
        tmp_path,
        """
        version: 1
        category: strong
        entries:
          - {canonical: fuck, forms: [fuck, damn]}
          - {canonical: shit, forms: [shit, damn]}
        """,
    )
    with pytest.raises(WordListError, match="claimed by both"):
        load_builtin_entries(tmp_path)


def test_rejects_form_in_never_match(tmp_path):
    write_data_dir(
        tmp_path,
        "version: 1\ncategory: strong\nentries:\n  - {canonical: fuck, forms: [fuck, x-ray]}\n",
    )
    with pytest.raises(WordListError, match="never_match"):
        load_builtin_entries(tmp_path)


@pytest.mark.parametrize("form", ["Fuck", "fu ck2", "-fuck", "fuck-", "f", "fu.ck"])
def test_rejects_bad_form_alphabet(tmp_path, form):
    write_data_dir(
        tmp_path,
        STRONG + f'  - {{canonical: fuck, forms: [fuck, "{form}"]}}\n',
    )
    with pytest.raises(WordListError):
        load_builtin_entries(tmp_path)


def test_rejects_multiword_form_on_non_phrase_entry(tmp_path):
    write_data_dir(
        tmp_path,
        STRONG + '  - {canonical: fuck, forms: [fuck, "fuck you"]}\n',
    )
    with pytest.raises(WordListError, match="multi-word form"):
        load_builtin_entries(tmp_path)


def test_phrase_entry_gets_its_canonical_injected_as_a_form(tmp_path):
    """Which is why there is no "phrase needs a multi-word form" validation."""
    write_data_dir(
        tmp_path,
        'version: 1\ncategory: strong\nentries:\n  - {canonical: "god damn", forms: [goddamn]}\n',
    )
    (entry,) = load_builtin_entries(tmp_path)
    assert entry.is_phrase
    assert "god damn" in entry.forms


def test_rejects_is_phrase_contradicting_the_canonical(tmp_path):
    write_data_dir(
        tmp_path,
        STRONG + "  - {canonical: fuck, forms: [fuck], is_phrase: true}\n",
    )
    with pytest.raises(WordListError, match="contradicts the canonical"):
        load_builtin_entries(tmp_path)


def test_rejects_focus_on_a_non_phrase(tmp_path):
    write_data_dir(
        tmp_path,
        STRONG + "  - {canonical: fuck, forms: [fuck], focus: [fuck]}\n",
    )
    with pytest.raises(WordListError, match="only valid on phrase entries"):
        load_builtin_entries(tmp_path)


def test_rejects_focus_word_outside_the_canonical(tmp_path):
    write_data_dir(
        tmp_path,
        "version: 1\ncategory: strong\nentries:\n"
        '  - {canonical: "god damn", forms: ["god damn"], focus: [shit]}\n',
    )
    with pytest.raises(WordListError, match="not part of the canonical"):
        load_builtin_entries(tmp_path)


def test_rejects_disabled_entry_without_a_note(tmp_path):
    write_data_dir(
        tmp_path,
        STRONG + "  - {canonical: sod, forms: [sod], enabled: false}\n",
    )
    with pytest.raises(WordListError, match="must carry a `note`"):
        load_builtin_entries(tmp_path)


def test_rejects_category_not_matching_the_filename(tmp_path):
    write_data_dir(
        tmp_path,
        "version: 1\ncategory: mild\nentries:\n  - {canonical: damn, forms: [damn]}\n",
        name="strong",
    )
    with pytest.raises(WordListError, match="but is named"):
        load_builtin_entries(tmp_path)


def test_rejects_unknown_category(tmp_path):
    write_data_dir(
        tmp_path,
        "version: 1\ncategory: rude\nentries:\n  - {canonical: damn, forms: [damn]}\n",
        name="rude",
    )
    with pytest.raises(WordListError):
        load_builtin_entries(tmp_path)


def test_rejects_unknown_schema_version(tmp_path):
    write_data_dir(
        tmp_path,
        "version: 99\ncategory: strong\nentries:\n  - {canonical: fuck, forms: [fuck]}\n",
    )
    with pytest.raises(WordListError):
        load_builtin_entries(tmp_path)


def test_rejects_unknown_field(tmp_path):
    write_data_dir(
        tmp_path,
        STRONG + "  - {canonical: fuck, forms: [fuck], colour: red}\n",
    )
    with pytest.raises(WordListError):
        load_builtin_entries(tmp_path)


def test_rejects_nested_compounds(tmp_path):
    write_data_dir(
        tmp_path,
        """
        version: 1
        category: strong
        entries:
          - canonical: fuck
            forms: [fuck]
            compounds:
              - canonical: motherfucker
                forms: [motherfucker]
                compounds:
                  - {canonical: deeper, forms: [deeper]}
        """,
    )
    with pytest.raises(WordListError, match="may not nest"):
        load_builtin_entries(tmp_path)


def test_rejects_empty_wordlist_directory(tmp_path):
    (tmp_path / "wordlists").mkdir()
    (tmp_path / "never_match.yaml").write_text(NEVER_MATCH_STUB, encoding="utf-8")
    with pytest.raises(WordListError, match="no word list files"):
        load_builtin_entries(tmp_path)
