"""`word_entries` / `profiles` / `whitelist` seeding and the DB-backed matcher."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from vidcleaner.db.models import Profile, WhitelistEntry
from vidcleaner.db.models import WordEntry as WordEntryRow
from vidcleaner.db.session import session_scope
from vidcleaner.matching.compiler import DEFAULT_CATEGORIES, WhitelistRule
from vidcleaner.matching.profile import (
    clear_matcher_cache,
    effective_entries,
    load_whitelist,
    matcher_for,
    profile_spec,
    seed_defaults,
    sync_builtin_word_entries,
)
from vidcleaner.matching.wordlists import load_builtin_entries, load_never_match


@pytest.fixture
def db(migrated):
    clear_matcher_cache()
    with session_scope() as session:
        yield session
    clear_matcher_cache()


# ------------------------------------------------------------------------ sync


def test_sync_inserts_every_builtin_entry(db):
    report = sync_builtin_word_entries(db)
    assert report.inserted == len(load_builtin_entries())
    assert report.updated == 0 and report.removed == 0
    assert db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "fuck")) is not None


def test_sync_is_idempotent(db):
    sync_builtin_word_entries(db)
    again = sync_builtin_word_entries(db)
    assert (again.inserted, again.updated, again.removed) == (0, 0, 0)


def test_sync_preserves_a_user_disabled_flag(db):
    sync_builtin_word_entries(db)
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "fuck"))
    row.enabled = False
    db.flush()

    report = sync_builtin_word_entries(db)
    assert report.enabled_preserved >= 1
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "fuck"))
    assert row.enabled is False, "sync overwrote a user choice"


def test_sync_preserves_a_user_enabling_a_shipped_off_word(db):
    sync_builtin_word_entries(db)
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "cock"))
    assert row.enabled is False  # ships disabled
    row.enabled = True
    db.flush()

    sync_builtin_word_entries(db)
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "cock"))
    assert row.enabled is True


def test_sync_removes_a_builtin_that_left_the_yaml(db):
    sync_builtin_word_entries(db)
    db.add(
        WordEntryRow(
            canonical="obsoleteword",
            category="strong",
            forms_json='["obsoleteword"]',
            is_builtin=True,
        )
    )
    db.flush()

    report = sync_builtin_word_entries(db)
    assert report.removed == 1
    assert db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "obsoleteword")) is None


def test_sync_never_touches_custom_entries(db):
    db.add(
        WordEntryRow(
            canonical="mycustomword",
            category="strong",
            forms_json='["mycustomword", "mycustomwords"]',
            is_builtin=False,
        )
    )
    db.flush()

    report = sync_builtin_word_entries(db)
    assert report.custom_kept == 1
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "mycustomword"))
    assert row is not None and row.is_builtin is False


def test_sync_updates_forms_when_the_yaml_changes(db):
    sync_builtin_word_entries(db)
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "fuck"))
    row.forms_json = '["fuck"]'
    db.flush()

    report = sync_builtin_word_entries(db)
    assert report.updated >= 1
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "fuck"))
    assert "fucking" in json.loads(row.forms_json)


# --------------------------------------------------------------------- seeding


def test_default_profile_is_seeded_once(db):
    first = seed_defaults(db)
    second = seed_defaults(db)
    assert first.id == second.id
    assert db.scalars(select(Profile)).all() == [first]
    assert set(json.loads(first.categories_json)) == DEFAULT_CATEGORIES


def test_seeded_profile_uses_the_agreed_categories(db):
    profile = seed_defaults(db)
    cats = set(json.loads(profile.categories_json))
    assert cats == {"strong", "slurs", "sexual", "religious"}
    assert "mild" not in cats


def test_default_global_whitelist_is_seeded(db):
    seed_defaults(db)
    rows = db.scalars(select(WhitelistEntry).where(WhitelistEntry.scope == "global")).all()
    assert len(rows) == len(load_never_match().default_whitelist)
    assert {r.canonical_word for r in rows} >= {"spic", "chink"}


def test_seeding_whitelist_is_idempotent(db):
    seed_defaults(db)
    seed_defaults(db)
    rows = db.scalars(select(WhitelistEntry)).all()
    assert len(rows) == len(load_never_match().default_whitelist)


# ------------------------------------------------------------- effective merge


def test_effective_entries_apply_db_enabled_overrides(db):
    sync_builtin_word_entries(db)
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "cock"))
    row.enabled = True
    db.flush()

    entries = {e.canonical: e for e in effective_entries(db)}
    assert entries["cock"].enabled is True


def test_effective_entries_keep_yaml_only_structure(db):
    """`focus`, `parent` and `note` have no DB columns, so YAML must win."""
    sync_builtin_word_entries(db)
    entries = {e.canonical: e for e in effective_entries(db)}
    assert entries["motherfucker"].parent == "fuck"
    assert entries["son of a bitch"].focus == ("bitch",)
    assert entries["cock"].note


def test_effective_entries_include_custom_rows(db):
    db.add(
        WordEntryRow(
            canonical="frobnicate",
            category="mild",
            forms_json='["frobnicate", "frobnicated"]',
            is_builtin=False,
        )
    )
    db.flush()
    entries = {e.canonical: e for e in effective_entries(db)}
    assert entries["frobnicate"].is_builtin is False
    assert "frobnicated" in entries["frobnicate"].forms


# ------------------------------------------------------------------- whitelist


def test_whitelist_scopes_are_filtered_by_id(db):
    db.add_all(
        [
            WhitelistEntry(scope="global", scope_id=None, canonical_word="ass"),
            WhitelistEntry(scope="title", scope_id=7, canonical_word="shit"),
            WhitelistEntry(scope="title", scope_id=8, canonical_word="bastard"),
            WhitelistEntry(scope="item", scope_id=3, canonical_word="fuck"),
        ]
    )
    db.flush()

    rules = load_whitelist(db, title_id=7, item_id=3)
    assert {r.canonical for r in rules} == {"ass", "shit", "fuck"}


def test_whitelist_without_scope_ids_is_global_only(db):
    db.add_all(
        [
            WhitelistEntry(scope="global", scope_id=None, canonical_word="ass"),
            WhitelistEntry(scope="title", scope_id=7, canonical_word="shit"),
        ]
    )
    db.flush()
    assert [r.canonical for r in load_whitelist(db)] == ["ass"]


def test_whitelist_is_returned_in_a_stable_order(db):
    db.add_all(
        [
            WhitelistEntry(scope="global", scope_id=None, canonical_word="shit"),
            WhitelistEntry(scope="global", scope_id=None, canonical_word="ass"),
        ]
    )
    db.flush()
    assert load_whitelist(db) == (WhitelistRule("ass"), WhitelistRule("shit"))


# --------------------------------------------------------------- matcher_for


def test_matcher_for_uses_the_seeded_default_profile(db):
    sync_builtin_word_entries(db)
    seed_defaults(db)
    m = matcher_for(db)
    assert m.profile.categories == DEFAULT_CATEGORIES
    assert [x.canonical for x in m.finditer("what the fuck")] == ["fuck"]
    assert [x.canonical for x in m.finditer("what the hell")] == []


def test_matcher_for_respects_a_db_disable(db):
    sync_builtin_word_entries(db)
    seed_defaults(db)
    row = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "fuck"))
    row.enabled = False
    db.flush()
    clear_matcher_cache()

    assert [x.canonical for x in matcher_for(db).finditer("what the fuck")] == []


def test_matcher_for_hash_differs_per_item_scope(db):
    sync_builtin_word_entries(db)
    seed_defaults(db)
    db.add(WhitelistEntry(scope="item", scope_id=3, canonical_word="fuck"))
    db.flush()

    plain = matcher_for(db).profile_hash
    scoped = matcher_for(db, item_id=3).profile_hash
    assert plain != scoped, "an item whitelist must change the profile hash"


def test_matcher_for_is_cached(db):
    sync_builtin_word_entries(db)
    seed_defaults(db)
    assert matcher_for(db) is matcher_for(db)


def test_profile_spec_falls_back_without_a_profile_row(db):
    spec = profile_spec(db)
    assert spec.categories == DEFAULT_CATEGORIES
    assert spec.pad_pre_ms == 80 and spec.pad_post_ms == 120


def test_profile_spec_reads_padding_and_categories_from_the_row(db):
    db.add(
        Profile(
            name="Strict",
            categories_json=json.dumps(["strong"]),
            extra_word_ids_json="[]",
            pad_pre_ms=150,
            pad_post_ms=250,
            is_default=True,
        )
    )
    db.flush()
    spec = profile_spec(db)
    assert spec.categories == frozenset({"strong"})
    assert (spec.pad_pre_ms, spec.pad_post_ms) == (150, 250)


def test_profile_spec_resolves_extra_word_ids_to_canonicals(db):
    sync_builtin_word_entries(db)
    damn = db.scalar(select(WordEntryRow).where(WordEntryRow.canonical == "damn"))
    db.add(
        Profile(
            name="Strong plus damn",
            categories_json=json.dumps(["strong"]),
            extra_word_ids_json=json.dumps([damn.id]),
            is_default=True,
        )
    )
    db.flush()
    assert profile_spec(db).extra_canonicals == frozenset({"damn"})


def test_profile_spec_takes_merge_gap_from_settings(db):
    from vidcleaner.settings_store import save_settings

    save_settings(db, {"merge_gap_ms": 500, "mute_censored_tokens": False})
    spec = profile_spec(db)
    assert spec.merge_gap_ms == 500
    assert spec.mute_censored_tokens is False


def test_malformed_json_falls_back_to_defaults(db):
    db.add(
        Profile(
            name="Broken",
            categories_json="{not json",
            extra_word_ids_json="also not json",
            is_default=True,
        )
    )
    db.flush()
    spec = profile_spec(db)
    assert spec.categories == DEFAULT_CATEGORIES
    assert spec.extra_canonicals == frozenset()
