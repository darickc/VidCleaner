"""Loading and validating the built-in word lists (PLAN.md §7).

The YAML format uses *explicit inflection tables*, never generic suffix rules:
generic rules yield junk forms and real false positives such as ``shitake``.

``compounds`` is authoring sugar only. The loader flattens each compound into a
sibling top-level entry carrying the same category, because:

1. ``detections.word_canonical`` feeds the §5 rollup query and a reviewer wants
   ``motherfucker: 12`` reported separately from ``fuck: 40``.
2. ``whitelist.canonical_word`` must be able to suppress ``bullshit`` without
   also suppressing ``shit``.
3. The compiled pattern needs them as standalone alternatives anyway -- that is
   exactly how the Scunthorpe problem is avoided.

Every validation failure is a hard error at load time. A broken word list must
never ship silently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from vidcleaner.db.constants import WORD_CATEGORIES
from vidcleaner.matching.normalize import fold, normalize

__all__ = [
    "NeverMatch",
    "WhitelistSeed",
    "WordEntry",
    "WordListError",
    "load_builtin_entries",
    "load_never_match",
    "wordlist_data_dir",
]

_FORM_RE = re.compile(r"^[a-z][a-z'\- ]*[a-z']$")
_SCHEMA_VERSION = 1


class WordListError(ValueError):
    """A word list file is malformed. Always fatal."""


class _RawEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical: str
    forms: list[str] = Field(min_length=1)
    compounds: list[_RawEntry] = Field(default_factory=list)
    is_phrase: bool | None = None
    focus: list[str] = Field(default_factory=list)
    enabled: bool = True
    note: str | None = None

    @field_validator("canonical")
    @classmethod
    def _canonical_normalized(cls, value: str) -> str:
        if normalize(value) != value:
            raise ValueError(f"canonical {value!r} is not already normalized")
        return value


class _WordListFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    category: str
    entries: list[_RawEntry] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _known_version(cls, value: int) -> int:
        if value != _SCHEMA_VERSION:
            raise ValueError(f"unsupported word list version {value}")
        return value

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in WORD_CATEGORIES:
            raise ValueError(f"unknown category {value!r}; expected one of {WORD_CATEGORIES}")
        return value


@dataclass(frozen=True, slots=True)
class WordEntry:
    """One matchable word or phrase, with its full inflection table."""

    canonical: str
    category: str
    forms: tuple[str, ...]
    """Deduped and sorted longest-first."""
    is_phrase: bool = False
    focus: tuple[str, ...] = ()
    """Phrases only: mute just these words rather than the whole span."""
    enabled: bool = True
    is_builtin: bool = True
    parent: str | None = None
    """Set on flattened compounds, for the Words UI's ``fuck > motherfucker`` nesting."""
    note: str | None = None

    @property
    def longest_form(self) -> int:
        return max(len(f) for f in self.forms)


@dataclass(frozen=True, slots=True)
class WhitelistSeed:
    canonical: str
    context_text: str | None = None


@dataclass(frozen=True, slots=True)
class NeverMatch:
    """The three roles of ``never_match.yaml``; see that file's header."""

    never_match: frozenset[str] = field(default_factory=frozenset)
    regression_corpus: tuple[str, ...] = ()
    default_whitelist: tuple[WhitelistSeed, ...] = ()


def wordlist_data_dir() -> Path:
    """Directory holding ``never_match.yaml`` and ``wordlists/``."""
    return Path(str(files("vidcleaner") / "data"))


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise WordListError(f"{path}: {exc}") from exc


def _check_form(form: str, *, where: str) -> None:
    if normalize(form) != form:
        raise WordListError(f"{where}: form {form!r} is not already normalized")
    if not _FORM_RE.match(form):
        raise WordListError(
            f"{where}: form {form!r} must be lowercase letters, spaces, hyphens and "
            "apostrophes, at least two characters, and may not start or end with a separator"
        )


def _flatten(raw: _RawEntry, category: str, *, parent: str | None, where: str) -> list[WordEntry]:
    forms = list(dict.fromkeys([raw.canonical, *raw.forms]))
    for form in forms:
        _check_form(form, where=f"{where}/{raw.canonical}")

    derived_phrase = " " in raw.canonical
    if raw.is_phrase is not None and raw.is_phrase != derived_phrase:
        raise WordListError(
            f"{where}/{raw.canonical}: is_phrase={raw.is_phrase} contradicts the canonical "
            f"({'contains' if derived_phrase else 'has no'} whitespace)"
        )

    # No "a phrase needs a multi-word form" check: `canonical` is auto-injected
    # into `forms` above, so for a phrase entry that is always satisfied.
    multiword = [f for f in forms if " " in f]
    if not derived_phrase and multiword:
        raise WordListError(
            f"{where}/{raw.canonical}: non-phrase entry has multi-word form(s) {multiword}"
        )

    if raw.focus:
        if not derived_phrase:
            raise WordListError(f"{where}/{raw.canonical}: focus is only valid on phrase entries")
        words = set(raw.canonical.split())
        unknown = [w for w in raw.focus if w not in words]
        if unknown:
            raise WordListError(
                f"{where}/{raw.canonical}: focus words {unknown} are not part of the canonical"
            )

    if not raw.enabled and not raw.note:
        raise WordListError(
            f"{where}/{raw.canonical}: an entry shipped disabled must carry a `note` saying why"
        )

    entry = WordEntry(
        canonical=raw.canonical,
        category=category,
        forms=tuple(sorted(set(forms), key=lambda f: (-len(f), f))),
        is_phrase=derived_phrase,
        focus=tuple(raw.focus),
        enabled=raw.enabled,
        is_builtin=True,
        parent=parent,
        note=raw.note,
    )
    out = [entry]
    for compound in raw.compounds:
        if compound.compounds:
            raise WordListError(f"{where}/{compound.canonical}: compounds may not nest")
        out.extend(_flatten(compound, category, parent=raw.canonical, where=where))
    return out


def _load_entries(data_dir: Path) -> tuple[WordEntry, ...]:
    wordlists = data_dir / "wordlists"
    paths = sorted(wordlists.glob("*.yaml"))
    if not paths:
        raise WordListError(f"no word list files found in {wordlists}")

    entries: list[WordEntry] = []
    for path in paths:
        payload = _read_yaml(path)
        try:
            parsed = _WordListFile.model_validate(payload)
        except Exception as exc:  # pydantic ValidationError
            raise WordListError(f"{path.name}: {exc}") from exc
        if parsed.category != path.stem:
            raise WordListError(
                f"{path.name}: declares category {parsed.category!r} but is named {path.stem!r}"
            )
        for raw in parsed.entries:
            entries.extend(_flatten(raw, parsed.category, parent=None, where=path.name))

    _check_global_invariants(entries, data_dir)
    return tuple(entries)


def _check_global_invariants(entries: Sequence[WordEntry], data_dir: Path) -> None:
    seen_canonical: dict[str, str] = {}
    for entry in entries:
        if entry.canonical in seen_canonical:
            raise WordListError(
                f"canonical {entry.canonical!r} appears in both "
                f"{seen_canonical[entry.canonical]!r} and {entry.category!r}; "
                "word_entries.canonical is UNIQUE"
            )
        seen_canonical[entry.canonical] = entry.category

    # The invariant that makes match -> entry attribution unambiguous by
    # construction, rather than by alternation ordering.
    owner: dict[str, str] = {}
    for entry in entries:
        for form in entry.forms:
            if form in owner and owner[form] != entry.canonical:
                raise WordListError(
                    f"form {form!r} is claimed by both {owner[form]!r} and {entry.canonical!r}"
                )
            owner[form] = entry.canonical

    blocked = _load_never_match(data_dir).never_match
    collisions = sorted({f for f in owner if fold(f) in blocked})
    if collisions:
        raise WordListError(f"forms {collisions} are also listed in never_match.yaml")


def _load_never_match(data_dir: Path) -> NeverMatch:
    path = data_dir / "never_match.yaml"
    payload = _read_yaml(path)
    if not isinstance(payload, dict):
        raise WordListError(f"{path.name}: expected a mapping at the top level")
    if payload.get("version") != _SCHEMA_VERSION:
        raise WordListError(f"{path.name}: unsupported version {payload.get('version')!r}")

    seeds: list[WhitelistSeed] = []
    for item in payload.get("default_whitelist") or []:
        if not isinstance(item, dict) or "canonical" not in item:
            raise WordListError(f"{path.name}: bad default_whitelist entry {item!r}")
        seeds.append(WhitelistSeed(item["canonical"], item.get("context_text")))

    return NeverMatch(
        never_match=frozenset(fold(w) for w in (payload.get("never_match") or [])),
        regression_corpus=tuple(payload.get("regression_corpus") or []),
        default_whitelist=tuple(seeds),
    )


@lru_cache(maxsize=4)
def _cached_entries(data_dir: str) -> tuple[WordEntry, ...]:
    return _load_entries(Path(data_dir))


@lru_cache(maxsize=4)
def _cached_never_match(data_dir: str) -> NeverMatch:
    return _load_never_match(Path(data_dir))


def load_builtin_entries(data_dir: Path | None = None) -> tuple[WordEntry, ...]:
    """Load every built-in entry, compounds already flattened. Cached."""
    return _cached_entries(str(data_dir or wordlist_data_dir()))


def load_never_match(data_dir: Path | None = None) -> NeverMatch:
    """Load ``never_match.yaml``. Cached."""
    return _cached_never_match(str(data_dir or wordlist_data_dir()))


def entries_by_category(entries: Iterable[WordEntry]) -> dict[str, list[WordEntry]]:
    out: dict[str, list[WordEntry]] = {c: [] for c in WORD_CATEGORIES}
    for entry in entries:
        out[entry.category].append(entry)
    return out
