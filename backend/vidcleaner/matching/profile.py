"""Bridging the `word_entries` / `profiles` / `whitelist` tables to a Matcher.

Layering, which matters because the DB is only a *partial* mirror of the YAML:

* **YAML is authoritative for structure** -- forms, ``is_phrase``, ``focus``,
  ``parent``, ``note``. The `word_entries` table has no columns for the last
  three, so rebuilding a matcher from rows alone would lose them.
* **The DB is authoritative for ``enabled``** once a row exists, so a user who
  turns `bloody` on keeps it on across upgrades, and for *custom* entries
  (``is_builtin=0``), which exist only in the database.

`matcher_for` therefore merges the two and caches on a cheap, fully hashable key
derived from the database state -- detection calls ``finditer`` once per subtitle
cue, thousands of times per job, so the pattern must not be recompiled per cue.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.models import Profile, WhitelistEntry
from vidcleaner.db.models import WordEntry as WordEntryRow
from vidcleaner.logging import get_logger
from vidcleaner.matching.compiler import (
    DEFAULT_CATEGORIES,
    Matcher,
    ProfileSpec,
    WhitelistRule,
    build_matcher,
)
from vidcleaner.matching.wordlists import WordEntry, load_builtin_entries, load_never_match
from vidcleaner.settings_store import AppSettings, load_settings

__all__ = [
    "SyncReport",
    "effective_entries",
    "load_whitelist",
    "matcher_for",
    "profile_spec",
    "seed_defaults",
    "sync_builtin_word_entries",
]

log = get_logger(__name__)

DEFAULT_PROFILE_NAME = "Default"


@dataclass(frozen=True, slots=True)
class SyncReport:
    inserted: int = 0
    updated: int = 0
    removed: int = 0
    custom_kept: int = 0
    enabled_preserved: int = 0


def _loads_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


# --------------------------------------------------------------------- seeding


def sync_builtin_word_entries(session: Session) -> SyncReport:
    """Mirror the YAML lists into `word_entries`, preserving user choices.

    Called at startup after ``upgrade_to_head`` -- deliberately not from an
    Alembic migration, because data seeding inside migrations ages badly.
    """
    builtin = {e.canonical: e for e in load_builtin_entries()}
    rows = {
        row.canonical: row
        for row in session.scalars(select(WordEntryRow).where(WordEntryRow.is_builtin.is_(True)))
    }
    custom = session.scalars(select(WordEntryRow).where(WordEntryRow.is_builtin.is_(False))).all()

    inserted = updated = removed = preserved = 0

    for canonical, entry in builtin.items():
        forms_json = json.dumps(list(entry.forms))
        row = rows.get(canonical)
        if row is None:
            session.add(
                WordEntryRow(
                    canonical=canonical,
                    category=entry.category,
                    forms_json=forms_json,
                    is_phrase=entry.is_phrase,
                    is_builtin=True,
                    enabled=entry.enabled,
                )
            )
            inserted += 1
            continue
        # `enabled` is never written back: the user owns it once the row exists.
        if row.enabled != entry.enabled:
            preserved += 1
        changed = (
            row.category != entry.category
            or row.forms_json != forms_json
            or row.is_phrase != entry.is_phrase
        )
        if changed:
            row.category = entry.category
            row.forms_json = forms_json
            row.is_phrase = entry.is_phrase
            updated += 1

    for canonical, row in rows.items():
        if canonical not in builtin:
            session.delete(row)
            removed += 1

    session.flush()
    report = SyncReport(inserted, updated, removed, len(custom), preserved)
    log.info(
        "wordlist.sync",
        inserted=report.inserted,
        updated=report.updated,
        removed=report.removed,
        custom_kept=report.custom_kept,
        enabled_preserved=report.enabled_preserved,
    )
    return report


def seed_defaults(session: Session) -> Profile:
    """Ensure a default profile and the shipped global whitelist seeds exist."""
    profile = session.scalars(select(Profile).where(Profile.is_default.is_(True))).first()
    if profile is None:
        profile = Profile(
            name=DEFAULT_PROFILE_NAME,
            categories_json=json.dumps(sorted(DEFAULT_CATEGORIES)),
            extra_word_ids_json="[]",
            pad_pre_ms=80,
            pad_post_ms=120,
            is_default=True,
        )
        session.add(profile)
        session.flush()
        log.info("profile.seeded", name=profile.name, categories=sorted(DEFAULT_CATEGORIES))

    existing = {
        (row.canonical_word, row.context_text)
        for row in session.scalars(select(WhitelistEntry).where(WhitelistEntry.scope == "global"))
    }
    added = 0
    for seed in load_never_match().default_whitelist:
        if (seed.canonical, seed.context_text) in existing:
            continue
        session.add(
            WhitelistEntry(
                scope="global",
                scope_id=None,
                canonical_word=seed.canonical,
                context_text=seed.context_text,
            )
        )
        added += 1
    if added:
        session.flush()
        log.info("whitelist.seeded", count=added)
    return profile


# ------------------------------------------------------------------- assembling


def effective_entries(session: Session) -> tuple[WordEntry, ...]:
    """YAML structure, with `enabled` overridden by the DB, plus custom rows."""
    overrides: dict[str, bool] = {}
    custom: list[WordEntry] = []
    for row in session.scalars(select(WordEntryRow)):
        if row.is_builtin:
            overrides[row.canonical] = row.enabled
            continue
        forms = [str(f) for f in _loads_list(row.forms_json)] or [row.canonical]
        custom.append(
            WordEntry(
                canonical=row.canonical,
                category=row.category,
                forms=tuple(sorted({row.canonical, *forms}, key=lambda f: (-len(f), f))),
                is_phrase=bool(row.is_phrase),
                enabled=row.enabled,
                is_builtin=False,
            )
        )

    merged = [
        entry
        if entry.canonical not in overrides
        else WordEntry(
            canonical=entry.canonical,
            category=entry.category,
            forms=entry.forms,
            is_phrase=entry.is_phrase,
            focus=entry.focus,
            enabled=overrides[entry.canonical],
            is_builtin=True,
            parent=entry.parent,
            note=entry.note,
        )
        for entry in load_builtin_entries()
    ]
    return tuple(merged) + tuple(custom)


def load_whitelist(
    session: Session, *, title_id: int | None = None, item_id: int | None = None
) -> tuple[WhitelistRule, ...]:
    """Global, title- and item-scoped rules, as a **union**.

    PLAN.md §7 writes "global -> title -> item", which reads like an override
    chain, but the schema has no negative form: a narrower scope can only add
    suppression, never restore a word. Recorded in the Decision Log; add a
    `mode` column in M5 if override semantics are ever wanted.
    """
    rows = session.scalars(select(WhitelistEntry)).all()
    rules: list[WhitelistRule] = []
    for row in rows:
        if row.scope == "global":
            keep = True
        elif row.scope == "title":
            keep = title_id is not None and row.scope_id == title_id
        elif row.scope == "item":
            keep = item_id is not None and row.scope_id == item_id
        else:  # pragma: no cover - constrained by db/constants
            keep = False
        if keep:
            rules.append(
                WhitelistRule(
                    canonical=row.canonical_word,
                    scope=row.scope,
                    scope_id=row.scope_id,
                    context_text=row.context_text,
                )
            )
    return tuple(sorted(rules, key=lambda r: (r.scope, r.scope_id or -1, r.canonical)))


def profile_spec(
    session: Session, *, profile_id: int | None = None, settings: AppSettings | None = None
) -> ProfileSpec:
    """Blend a `profiles` row with the operational settings that affect muting."""
    app = settings or load_settings(session)
    row: Profile | None = None
    if profile_id is not None:
        row = session.get(Profile, profile_id)
    if row is None:
        row = session.scalars(select(Profile).where(Profile.is_default.is_(True))).first()

    if row is None:
        categories = DEFAULT_CATEGORIES
        extras: frozenset[str] = frozenset()
        name, pad_pre, pad_post = DEFAULT_PROFILE_NAME, app.pad_pre_ms, app.pad_post_ms
    else:
        cats = {str(c) for c in _loads_list(row.categories_json)}
        categories = frozenset(cats) if cats else DEFAULT_CATEGORIES
        ids = [int(i) for i in _loads_list(row.extra_word_ids_json) if isinstance(i, int)]
        extras = frozenset(_canonicals_for_ids(session, ids))
        name, pad_pre, pad_post = row.name, row.pad_pre_ms, row.pad_post_ms

    return ProfileSpec(
        name=name,
        categories=categories,
        extra_canonicals=extras,
        pad_pre_ms=pad_pre,
        pad_post_ms=pad_post,
        merge_gap_ms=app.merge_gap_ms,
        mute_censored_tokens=app.mute_censored_tokens,
    )


def _canonicals_for_ids(session: Session, ids: Sequence[int]) -> list[str]:
    if not ids:
        return []
    rows = session.scalars(select(WordEntryRow).where(WordEntryRow.id.in_(list(ids)))).all()
    return [row.canonical for row in rows]


@lru_cache(maxsize=16)
def _build_cached(
    profile: ProfileSpec,
    entries: tuple[WordEntry, ...],
    whitelist: tuple[WhitelistRule, ...],
) -> Matcher:
    return build_matcher(entries, profile, whitelist)


def matcher_for(
    session: Session,
    *,
    title_id: int | None = None,
    item_id: int | None = None,
    profile_id: int | None = None,
    settings: AppSettings | None = None,
) -> Matcher:
    """The effective matcher for one media item.

    Its ``profile_hash`` is the value written to the output's
    ``VIDCLEANER_PROFILE_HASH`` tag, and it is **item-specific** because
    item/title whitelists feed the hash. See :func:`compiler.profile_hash`.
    """
    return _build_cached(
        profile_spec(session, profile_id=profile_id, settings=settings),
        effective_entries(session),
        load_whitelist(session, title_id=title_id, item_id=item_id),
    )


def clear_matcher_cache() -> None:
    """Drop the compiled-matcher cache (after editing words or the whitelist)."""
    _build_cached.cache_clear()


def ensure_seed_data() -> SyncReport:
    """Sync the word lists and seed the default profile/whitelist.

    Called at startup, after migrations. Ownership avoids a race between the two
    processes: the api does it whenever it runs, and a worker-only deployment
    does it instead (see ``worker_main``).
    """
    from vidcleaner.db.session import session_scope  # noqa: PLC0415

    with session_scope() as session:
        report = sync_builtin_word_entries(session)
        seed_defaults(session)
    clear_matcher_cache()
    return report
