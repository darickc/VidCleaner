"""§9.5's Words & Profiles screen: word entries, profiles and the whitelist.

Three tables, one router, because the page edits all three together and every write
here changes the same thing -- the mute set, and therefore
``VIDCLEANER_PROFILE_HASH``. Keeping them together lets that shared consequence be
stated once (:func:`_edited`).

**The word list is a merge, not a table.** ``matching/wordlists.py`` owns structure
(forms, ``is_phrase``, ``focus``, ``parent``, ``note``) and the database owns
``enabled`` plus any custom entry; ``profile.effective_entries`` already performs that
merge. This module only re-attaches the row ids the merge drops, because a UI needs
something to PATCH. Consequence worth stating rather than hiding: a **custom** entry
has nowhere to keep ``focus``/``parent``/``note`` -- there are no columns -- so those
come back null for one.

Handlers are sync ``def`` for the same reason as :mod:`api.actions`: FastAPI runs them
in a threadpool, so the blocking database costs a worker thread, not the event loop.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.constants import WHITELIST_SCOPES, WORD_CATEGORIES
from vidcleaner.db.models import MediaItem, Profile, Title, WhitelistEntry
from vidcleaner.db.models import WordEntry as WordEntryRow
from vidcleaner.db.session import get_db
from vidcleaner.logging import get_logger
from vidcleaner.matching.compiler import DEFAULT_CATEGORIES, default_profile
from vidcleaner.matching.normalize import normalize
from vidcleaner.matching.profile import clear_matcher_cache, effective_entries
from vidcleaner.matching.wordlists import WordListError, check_form, load_never_match

router = APIRouter(tags=["words"])
DbSession = Annotated[Session, Depends(get_db)]
log = get_logger(__name__)


def _edited(reason: str, **fields: object) -> None:
    """Every write in this module invalidates the compiled matcher.

    Note this is a *memory* concern, not a correctness one: ``matcher_for`` re-reads
    these tables on every call and ``_build_cached`` is keyed on the resulting
    **values**, so the cache is self-invalidating and a stale matcher cannot be
    served. Dropping it anyway keeps the 16-entry LRU from filling with dead keys as
    a user works through the Words page. Do not "fix" this into a correctness fence.
    """
    clear_matcher_cache()
    log.info(f"words.{reason}", **fields)


# ------------------------------------------------------------------------ words


class WordRow(BaseModel):
    id: int | None = None
    """``None`` only for a built-in the seeder has not mirrored yet (first boot)."""
    canonical: str
    category: str
    forms: list[str] = Field(default_factory=list)
    is_phrase: bool = False
    is_builtin: bool = True
    enabled: bool = True
    focus: list[str] = Field(default_factory=list)
    parent: str | None = None
    """Set on flattened compounds, for the page's ``fuck > motherfucker`` nesting."""
    note: str | None = None
    """Why a built-in ships disabled. Custom entries have no column for it."""


class WordList(BaseModel):
    categories: list[str] = Field(default_factory=list)
    """``WORD_CATEGORIES`` in §2's order, so the page need not hardcode it."""
    default_categories: list[str] = Field(default_factory=list)
    words: list[WordRow] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    enabled_counts: dict[str, int] = Field(default_factory=dict)


class WordCreate(BaseModel):
    canonical: str = Field(min_length=1, max_length=200)
    category: Literal["strong", "mild", "religious", "slurs", "sexual"] = "strong"
    forms: list[str] = Field(default_factory=list)
    """The canonical is always included; explicit inflections only (§7 forbids
    generic suffix rules, which produce junk forms and real false positives)."""


class WordPatch(BaseModel):
    enabled: bool


def _row_ids(session: Session) -> dict[str, int]:
    return {
        canonical: row_id
        for row_id, canonical in session.execute(
            select(WordEntryRow.id, WordEntryRow.canonical)
        ).all()
    }


@router.get("/words", response_model=WordList)
def list_words(db: DbSession) -> WordList:
    """Every effective entry, with the row id the page needs to PATCH it."""
    ids = _row_ids(db)
    words = [
        WordRow(
            id=ids.get(entry.canonical),
            canonical=entry.canonical,
            category=entry.category,
            forms=list(entry.forms),
            is_phrase=entry.is_phrase,
            is_builtin=entry.is_builtin,
            enabled=entry.enabled,
            focus=list(entry.focus),
            parent=entry.parent,
            note=entry.note,
        )
        for entry in effective_entries(db)
    ]
    counts = {c: 0 for c in WORD_CATEGORIES}
    enabled = {c: 0 for c in WORD_CATEGORIES}
    for word in words:
        counts[word.category] = counts.get(word.category, 0) + 1
        if word.enabled:
            enabled[word.category] = enabled.get(word.category, 0) + 1
    return WordList(
        categories=list(WORD_CATEGORIES),
        default_categories=sorted(DEFAULT_CATEGORIES),
        words=sorted(words, key=lambda w: (w.category, w.canonical)),
        counts=counts,
        enabled_counts=enabled,
    )


@router.patch("/words/{word_id}", response_model=WordRow)
def patch_word(word_id: int, patch: Annotated[WordPatch, Body()], db: DbSession) -> WordRow:
    """Turn one entry on or off. The only mutable field of a built-in."""
    row = db.get(WordEntryRow, word_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no word entry {word_id}")
    row.enabled = patch.enabled
    db.flush()
    _edited("toggled", canonical=row.canonical, enabled=row.enabled)
    return _row_to_model(db, row)


@router.post("/words", response_model=WordRow, status_code=201)
def create_word(request: Annotated[WordCreate, Body()], db: DbSession) -> WordRow:
    """Add a custom word or phrase (§2's "custom words/phrases").

    Validated with the *same* rules as the YAML loader -- a custom entry the loader
    would reject is a custom entry that behaves differently from every built-in one.
    """
    canonical = normalize(request.canonical)
    forms = list(dict.fromkeys([canonical, *(normalize(f) for f in request.forms)]))
    try:
        for form in forms:
            check_form(form, where="custom word")
    except WordListError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    is_phrase = " " in canonical
    multiword = [f for f in forms if " " in f]
    if not is_phrase and multiword:
        raise HTTPException(
            status_code=422,
            detail=f"non-phrase entry has multi-word form(s) {multiword}",
        )

    never = load_never_match().never_match
    collisions = sorted(set(forms) & never)
    if collisions:
        raise HTTPException(
            status_code=422,
            detail=f"{collisions} are on the never-match list and cannot be matched",
        )

    existing = effective_entries(db)
    if any(e.canonical == canonical for e in existing):
        raise HTTPException(status_code=409, detail=f"{canonical!r} already exists")
    claimed = {form: e.canonical for e in existing for form in e.forms}
    taken = {f: claimed[f] for f in forms if f in claimed}
    if taken:
        # Two entries owning one form would make the form -> canonical map
        # order-dependent, so a hit would be attributed to whichever loaded last.
        raise HTTPException(
            status_code=409, detail=f"form(s) already claimed by another entry: {taken}"
        )

    row = WordEntryRow(
        canonical=canonical,
        category=request.category,
        forms_json=json.dumps(forms),
        is_phrase=is_phrase,
        is_builtin=False,
        enabled=True,
    )
    db.add(row)
    db.flush()
    _edited("created", canonical=canonical, category=row.category, forms=len(forms))
    return _row_to_model(db, row)


@router.delete("/words/{word_id}", status_code=204)
def delete_word(word_id: int, db: DbSession) -> None:
    """Custom entries only.

    A built-in cannot be deleted: ``sync_builtin_word_entries`` mirrors the YAML at
    every startup and would resurrect the row on the next restart, so the button
    would appear to work and then silently undo itself. Disable it instead.
    """
    row = db.get(WordEntryRow, word_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no word entry {word_id}")
    if row.is_builtin:
        raise HTTPException(
            status_code=409,
            detail="a built-in entry cannot be deleted (startup would re-seed it); disable it",
        )
    canonical = row.canonical
    db.delete(row)
    db.flush()
    _edited("deleted", canonical=canonical)


def _row_to_model(session: Session, row: WordEntryRow) -> WordRow:
    """Re-read through the merge, so the response carries the YAML-only fields."""
    for entry in effective_entries(session):
        if entry.canonical == row.canonical:
            return WordRow(
                id=row.id,
                canonical=entry.canonical,
                category=entry.category,
                forms=list(entry.forms),
                is_phrase=entry.is_phrase,
                is_builtin=entry.is_builtin,
                enabled=entry.enabled,
                focus=list(entry.focus),
                parent=entry.parent,
                note=entry.note,
            )
    return WordRow(  # pragma: no cover - a row that exists is always in the merge
        id=row.id,
        canonical=row.canonical,
        category=row.category,
        forms=[str(f) for f in json.loads(row.forms_json or "[]")],
        is_phrase=bool(row.is_phrase),
        is_builtin=bool(row.is_builtin),
        enabled=bool(row.enabled),
    )


# --------------------------------------------------------------------- profiles


class ProfileRow(BaseModel):
    id: int
    name: str
    categories: list[str] = Field(default_factory=list)
    extra_word_ids: list[int] = Field(default_factory=list)
    pad_pre_ms: int = 80
    pad_post_ms: int = 120
    is_default: bool = False
    titles: int = 0
    """How many titles override to this profile -- what makes a delete consequential."""


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    categories: list[str] = Field(default_factory=lambda: sorted(DEFAULT_CATEGORIES))
    extra_word_ids: list[int] = Field(default_factory=list)
    pad_pre_ms: int = Field(default=80, ge=0, le=2000)
    pad_post_ms: int = Field(default=120, ge=0, le=2000)
    is_default: bool = False


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    categories: list[str] | None = None
    extra_word_ids: list[int] | None = None
    pad_pre_ms: int | None = Field(default=None, ge=0, le=2000)
    pad_post_ms: int | None = Field(default=None, ge=0, le=2000)
    is_default: bool | None = None


def _loads_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover - defensive, mirrors profile.py
        return []
    return value if isinstance(value, list) else []


def _title_counts(session: Session) -> dict[int, int]:
    counts: dict[int, int] = {}
    for (profile_id,) in session.execute(
        select(Title.profile_id).where(Title.profile_id.is_not(None))
    ).all():
        counts[profile_id] = counts.get(profile_id, 0) + 1
    return counts


def _profile_row(row: Profile, titles: int = 0) -> ProfileRow:
    return ProfileRow(
        id=row.id,
        name=row.name,
        categories=sorted(str(c) for c in _loads_list(row.categories_json)),
        extra_word_ids=[int(i) for i in _loads_list(row.extra_word_ids_json) if isinstance(i, int)],
        pad_pre_ms=row.pad_pre_ms,
        pad_post_ms=row.pad_post_ms,
        is_default=row.is_default,
        titles=titles,
    )


def _validate_categories(categories: list[str]) -> list[str]:
    try:
        # `default_profile` already rejects an unknown category; reusing it keeps one
        # definition of "which categories exist" rather than a second list here.
        default_profile(categories)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return sorted(set(categories))


def _validate_extras(session: Session, ids: list[int]) -> list[int]:
    if not ids:
        return []
    known = {
        row_id
        for (row_id,) in session.execute(
            select(WordEntryRow.id).where(WordEntryRow.id.in_(list(ids)))
        ).all()
    }
    missing = sorted(set(ids) - known)
    if missing:
        raise HTTPException(status_code=422, detail=f"no word entries {missing}")
    return sorted(set(ids))


def _make_default(session: Session, profile: Profile) -> None:
    """`profiles.is_default` has no uniqueness constraint and ``profile_spec`` takes
    ``.first()``, so two defaults would make the effective profile depend on row
    order. Clearing the others here is what keeps that from happening."""
    for other in session.scalars(select(Profile).where(Profile.id != profile.id)):
        other.is_default = False
    profile.is_default = True


@router.get("/profiles", response_model=list[ProfileRow])
def list_profiles(db: DbSession) -> list[ProfileRow]:
    """What the Title page's profile dropdown reads."""
    counts = _title_counts(db)
    rows = db.scalars(select(Profile).order_by(Profile.is_default.desc(), Profile.name)).all()
    return [_profile_row(row, counts.get(row.id, 0)) for row in rows]


@router.post("/profiles", response_model=ProfileRow, status_code=201)
def create_profile(request: Annotated[ProfileCreate, Body()], db: DbSession) -> ProfileRow:
    name = request.name.strip()
    if db.scalars(select(Profile).where(Profile.name == name)).first() is not None:
        raise HTTPException(status_code=409, detail=f"a profile named {name!r} already exists")
    categories = _validate_categories(request.categories)
    extras = _validate_extras(db, request.extra_word_ids)

    row = Profile(
        name=name,
        categories_json=json.dumps(categories),
        extra_word_ids_json=json.dumps(extras),
        pad_pre_ms=request.pad_pre_ms,
        pad_post_ms=request.pad_post_ms,
        is_default=False,
    )
    db.add(row)
    db.flush()
    if request.is_default:
        _make_default(db, row)
        db.flush()
    _edited("profile_created", profile=row.name, categories=categories)
    return _profile_row(row)


@router.patch("/profiles/{profile_id}", response_model=ProfileRow)
def patch_profile(
    profile_id: int, patch: Annotated[ProfilePatch, Body()], db: DbSession
) -> ProfileRow:
    row = db.get(Profile, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no profile {profile_id}")

    if patch.name is not None:
        name = patch.name.strip()
        clash = db.scalars(
            select(Profile).where(Profile.name == name, Profile.id != profile_id)
        ).first()
        if clash is not None:
            raise HTTPException(status_code=409, detail=f"a profile named {name!r} already exists")
        row.name = name
    if patch.categories is not None:
        row.categories_json = json.dumps(_validate_categories(patch.categories))
    if patch.extra_word_ids is not None:
        row.extra_word_ids_json = json.dumps(_validate_extras(db, patch.extra_word_ids))
    if patch.pad_pre_ms is not None:
        row.pad_pre_ms = patch.pad_pre_ms
    if patch.pad_post_ms is not None:
        row.pad_post_ms = patch.pad_post_ms
    if patch.is_default is True:
        _make_default(db, row)
    elif patch.is_default is False and row.is_default:
        raise HTTPException(
            status_code=422,
            detail="make another profile the default rather than leaving none",
        )
    db.flush()
    _edited("profile_updated", profile=row.name)
    return _profile_row(row, _title_counts(db).get(row.id, 0))


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: int, db: DbSession) -> None:
    """Titles pointing here fall back to the default (`ondelete="SET NULL"`).

    The default itself cannot go: ``profile_spec`` falls back to it for every title
    without an override, so deleting it would silently change the mute set of the
    whole library.
    """
    row = db.get(Profile, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no profile {profile_id}")
    if row.is_default:
        raise HTTPException(
            status_code=409, detail="the default profile cannot be deleted; make another default"
        )
    name = row.name
    affected = _title_counts(db).get(profile_id, 0)
    db.delete(row)
    db.flush()
    _edited("profile_deleted", profile=name, titles_reset=affected)


# -------------------------------------------------------------------- whitelist


class WhitelistRow(BaseModel):
    id: int
    scope: str
    scope_id: int | None = None
    canonical_word: str
    context_text: str | None = None
    label: str | None = None
    """What the scope points at, resolved for display ("Pluribus", "S01E01")."""


class WhitelistCreate(BaseModel):
    canonical_word: str = Field(min_length=1, max_length=200)
    scope: Literal["global", "title", "item"] = "global"
    scope_id: int | None = None
    context_text: str | None = None


def _whitelist_labels(session: Session, rows: list[WhitelistEntry]) -> dict[int, str]:
    """One query per scope kind, not one per row."""
    title_ids = {r.scope_id for r in rows if r.scope == "title" and r.scope_id is not None}
    item_ids = {r.scope_id for r in rows if r.scope == "item" and r.scope_id is not None}
    labels: dict[int, str] = {}
    if title_ids:
        for title in session.scalars(select(Title).where(Title.id.in_(title_ids))):
            labels[-title.id] = title.title
    if item_ids:
        from vidcleaner.api.views import item_label  # noqa: PLC0415 - avoids a cycle

        for item in session.scalars(select(MediaItem).where(MediaItem.id.in_(item_ids))):
            labels[item.id] = item_label(item, session.get(Title, item.title_id))
    out: dict[int, str] = {}
    for row in rows:
        if row.scope == "title" and row.scope_id is not None:
            out[row.id] = labels.get(-row.scope_id, f"title {row.scope_id}")
        elif row.scope == "item" and row.scope_id is not None:
            out[row.id] = labels.get(row.scope_id, f"item {row.scope_id}")
    return out


@router.get("/whitelist", response_model=list[WhitelistRow])
def list_whitelist(db: DbSession) -> list[WhitelistRow]:
    """Every rule, at every scope.

    §9.4 shows only the rules in scope for one item; the Words page needs the whole
    set, because a global rule added months ago is exactly the thing a user comes here
    to find and remove.
    """
    rows = list(
        db.scalars(
            select(WhitelistEntry).order_by(
                WhitelistEntry.scope, WhitelistEntry.canonical_word, WhitelistEntry.id
            )
        ).all()
    )
    labels = _whitelist_labels(db, rows)
    return [
        WhitelistRow(
            id=row.id,
            scope=row.scope,
            scope_id=row.scope_id,
            canonical_word=row.canonical_word,
            context_text=row.context_text,
            label=labels.get(row.id),
        )
        for row in rows
    ]


@router.post("/whitelist", response_model=WhitelistRow, status_code=201)
def create_whitelist(request: Annotated[WhitelistCreate, Body()], db: DbSession) -> WhitelistRow:
    """Add a rule from the Words page, at any scope.

    ``POST /items/{id}/whitelist`` stays the review flow -- it derives the scope id
    from the detection's own item and can queue the reprocess. This one is the
    library-wide editor, so the scope id is explicit and nothing is queued: a global
    rule can touch thousands of files and the user picks when that work happens.
    """
    if request.scope not in WHITELIST_SCOPES:  # pragma: no cover - Literal-constrained
        raise HTTPException(status_code=422, detail="unknown scope")
    scope_id = request.scope_id
    if request.scope == "global":
        scope_id = None
    elif scope_id is None:
        raise HTTPException(status_code=422, detail=f"scope {request.scope!r} needs a scope_id")
    elif request.scope == "title" and db.get(Title, scope_id) is None:
        raise HTTPException(status_code=422, detail=f"no title {scope_id}")
    elif request.scope == "item" and db.get(MediaItem, scope_id) is None:
        raise HTTPException(status_code=422, detail=f"no media item {scope_id}")

    word = normalize(request.canonical_word)
    context = request.context_text.strip() if request.context_text else None
    existing = db.scalars(
        select(WhitelistEntry).where(
            WhitelistEntry.scope == request.scope,
            WhitelistEntry.canonical_word == word,
            WhitelistEntry.scope_id.is_(None)
            if scope_id is None
            else WhitelistEntry.scope_id == scope_id,
        )
    ).first()
    row = existing or WhitelistEntry(
        scope=request.scope, scope_id=scope_id, canonical_word=word, context_text=context
    )
    if existing is None:
        db.add(row)
        db.flush()
        _edited("whitelisted", word=word, scope=request.scope, scope_id=scope_id)
    labels = _whitelist_labels(db, [row])
    return WhitelistRow(
        id=row.id,
        scope=row.scope,
        scope_id=row.scope_id,
        canonical_word=row.canonical_word,
        context_text=row.context_text,
        label=labels.get(row.id),
    )
