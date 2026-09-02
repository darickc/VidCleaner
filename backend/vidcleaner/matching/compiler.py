"""Compiling word lists into one alternation pattern, plus profile hashing.

Two corrections to PLAN.md §7, both verified empirically (see §14 Decision Log):

**No ``\\b``.** §7 specifies ``\\b(?:...)\\b``, but ``\\b`` after an apostrophe
requires a following word character, so ``re.compile(r"\\b(?:fuckin')\\b")`` does
**not** match ``"he was fuckin' tired"`` -- and the M1 test media contains
``friggin'``. The boundaries are asymmetric lookarounds instead:
``(?<!\\w)(?:...)(?!\\w)``. These still reject every classic substring case
(Scunthorpe, assassin, class, bass, cassette, shell, cocktail, hello, Uranus,
shiitake) with no help from ``never_match.yaml``.

**Bounded phrase separator.** §7's ``[\\s\\-']+`` includes ``\\n``, so ``god damn``
matched across a subtitle line break in ``"oh my god\\ndamn that hurt"``. The
separator is ``[ \\t\\xa0\\-'’]{1,3}`` -- no newline, bounded length.

Whitelisted canonicals deliberately stay compiled **in**. §5's own rollup query
filters ``WHERE whitelisted=0``, which only makes sense if such rows exist, and
§9.4's review UI needs them in order to offer un-whitelisting. Suppression is
applied by :meth:`Matcher.suppressed`, not by omission.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from vidcleaner.db.constants import WORD_CATEGORIES
from vidcleaner.matching.normalize import (
    CensorCandidate,
    fold,
)
from vidcleaner.matching.wordlists import (
    NeverMatch,
    WordEntry,
    load_builtin_entries,
    load_never_match,
)

__all__ = [
    "DEFAULT_CATEGORIES",
    "Match",
    "Matcher",
    "ProfileSpec",
    "WhitelistRule",
    "build_matcher",
    "compile_pattern",
    "mask_text",
    "profile_hash",
    "select_entries",
]

#: Enabled in the default profile (decided 2026-09-01). `mild` is off: muting
#: damn/hell/crap roughly triples the cuts in a typical film for words most
#: viewers do not object to. Precision within the enabled categories comes from
#: per-entry `enabled: false`, not from category toggles.
DEFAULT_CATEGORIES: frozenset[str] = frozenset({"strong", "slurs", "sexual", "religious"})

#: Algorithm version for `profile_hash`. Bump when the *matching logic* changes
#: but the word data does not; that re-processes the library, which is correct.
ALGO_VERSION = 1

_PHRASE_SEP = r"[ \t\xa0\-'’]{1,3}"
_WORD_SEP = r"['’\-]?"
_TRAIL_APOS = r"['’]?"
_SPLIT_RE = re.compile(r"[\s\-'’]+")


def _form_pattern(form: str, *, is_phrase: bool) -> str:
    """Compile one surface form to a regex fragment (no boundaries, no group)."""
    parts = [p for p in _SPLIT_RE.split(form) if p]
    if not parts:
        raise ValueError(f"form {form!r} has no word characters")
    if is_phrase and len(parts) > 1:
        return _PHRASE_SEP.join(re.escape(p) for p in parts)
    core = _WORD_SEP.join(re.escape(p) for p in parts)
    if form[-1] in "'’":
        core += _TRAIL_APOS
    return core


def compile_pattern(entries: Sequence[WordEntry]) -> re.Pattern[str] | None:
    """One alternation over every entry, longest form first.

    Returns ``None`` for an empty entry set. This matters: ``re.compile(
    r"(?<!\\w)(?:)(?!\\w)")`` matches at *every position*, so a profile with no
    categories enabled would flag the whole file.

    Ordering is longest-first per §7, but correctness does not depend on it --
    the trailing ``(?!\\w)`` forces backtracking into a longer alternative.
    Ordering is for determinism and to make the first success the longest.
    """
    if not entries:
        return None
    ordered = sorted(entries, key=lambda e: (-e.longest_form, e.canonical))
    groups = []
    for i, entry in enumerate(ordered):
        alts = "|".join(
            _form_pattern(f, is_phrase=entry.is_phrase)
            for f in sorted(set(entry.forms), key=lambda f: (-len(f), f))
        )
        groups.append(f"(?P<w{i}>{alts})")
    return re.compile(r"(?<!\w)(?:" + "|".join(groups) + r")(?!\w)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Match:
    """One hit, already attributed to the entry that owns the matched form."""

    canonical: str
    category: str
    raw: str
    start: int
    end: int
    entry: WordEntry

    @property
    def is_phrase(self) -> bool:
        return self.entry.is_phrase


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    """The subset of a `profiles` row that affects the mute set."""

    name: str = "Default"
    categories: frozenset[str] = DEFAULT_CATEGORIES
    extra_canonicals: frozenset[str] = frozenset()
    pad_pre_ms: int = 80
    pad_post_ms: int = 120
    merge_gap_ms: int = 250
    mute_censored_tokens: bool = True


@dataclass(frozen=True, slots=True)
class WhitelistRule:
    canonical: str
    scope: Literal["global", "title", "item"] = "global"
    scope_id: int | None = None
    context_text: str | None = None
    """``None`` suppresses the canonical outright within the scope."""


@dataclass(frozen=True, slots=True)
class Matcher:
    """A compiled, profile-specific matcher. Build via :func:`build_matcher`."""

    pattern: re.Pattern[str] | None
    entries: tuple[WordEntry, ...]
    profile: ProfileSpec
    profile_hash: str
    never_match: frozenset[str]
    _by_group: Mapping[str, WordEntry] = field(repr=False, default_factory=dict)
    _by_canonical: Mapping[str, WordEntry] = field(repr=False, default_factory=dict)
    _suppress_all: frozenset[str] = field(repr=False, default_factory=frozenset)
    _suppress_ctx: Mapping[str, tuple[str, ...]] = field(repr=False, default_factory=dict)
    by_letter_len: Mapping[tuple[str, int], tuple[CensorCandidate, ...]] = field(
        repr=False, default_factory=dict
    )
    by_letter: Mapping[str, tuple[CensorCandidate, ...]] = field(repr=False, default_factory=dict)

    def finditer(self, text: str):
        """Yield every :class:`Match` in ``text``, leftmost-longest."""
        if self.pattern is None or not text:
            return
        for m in self.pattern.finditer(text):
            group = m.lastgroup
            if group is None:  # pragma: no cover - structurally impossible
                continue
            entry = self._by_group[group]
            yield Match(
                canonical=entry.canonical,
                category=entry.category,
                raw=m.group(0),
                start=m.start(),
                end=m.end(),
                entry=entry,
            )

    def forms_of(self, canonical: str) -> tuple[str, ...]:
        """Every authored form of ``canonical``.

        Used by the detector's timing selector: comparing a subtitle hit against
        the entry's *whole* form table rather than the matched surface string is
        what makes subtitle ``fuck`` pair with Whisper ``fucking``
        (``fuzz.ratio("fuck", "fucking")`` is only 72.7). It never expands the
        word list -- every target is already an authored form.
        """
        entry = self._by_canonical.get(canonical)
        return entry.forms if entry else ()

    def entry_for(self, canonical: str) -> WordEntry | None:
        return self._by_canonical.get(canonical)

    def suppressed(self, match: Match, context: str = "") -> bool:
        """True when the whitelist suppresses this hit in the current scope."""
        if match.canonical in self._suppress_all:
            return True
        needles = self._suppress_ctx.get(match.canonical)
        if not needles:
            return False
        haystack = fold(context or match.raw)
        return any(n in haystack for n in needles)


def select_entries(entries: Iterable[WordEntry], profile: ProfileSpec) -> tuple[WordEntry, ...]:
    """Entries active for ``profile``: enabled, and in an enabled category."""
    return tuple(
        e
        for e in entries
        if e.enabled
        and (e.category in profile.categories or e.canonical in profile.extra_canonicals)
    )


def _sha1(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(blob.encode("ascii")).hexdigest()


def profile_hash(
    profile: ProfileSpec,
    entries: Sequence[WordEntry],
    whitelist: Sequence[WhitelistRule] = (),
    never_match: frozenset[str] = frozenset(),
) -> str:
    """Answer exactly one question: would re-running produce the same mute set?

    Consumed by PLAN.md §4's ``VIDCLEANER_PROFILE_HASH`` tag and §6.1's
    ``already_clean`` short-circuit.

    **This hash is per media item, not per profile.** Item- and title-scoped
    whitelist rules go into it, so `matcher_for(session, title_id=..., item_id=...)`
    returns a matcher whose ``profile_hash`` is the effective one for that item.
    Leaving them out would make "add an item whitelist, hit Reprocess"
    short-circuit straight to ``already_clean`` -- a subtle correctness trap.

    Deliberately excluded: the profile's name, row ids, ``is_builtin``, the STT
    model, and the codec policy. Including the STT model would invalidate every
    already-cleaned file the first time someone tried `medium` against `turbo`,
    and §4 calls this a *profile* hash.
    """
    doc = {
        "v": ALGO_VERSION,
        "categories": sorted(profile.categories),
        "extra": sorted(profile.extra_canonicals),
        "pad_pre_ms": profile.pad_pre_ms,
        "pad_post_ms": profile.pad_post_ms,
        "merge_gap_ms": profile.merge_gap_ms,
        "mute_censored_tokens": profile.mute_censored_tokens,
        "entries": sorted(
            [e.canonical, e.category, sorted(e.forms), sorted(e.focus)] for e in entries
        ),
        "whitelist": sorted(
            [
                r.scope,
                r.scope_id if r.scope_id is not None else -1,
                r.canonical,
                r.context_text or "",
            ]
            for r in whitelist
        ),
        "never_match": _sha1(sorted(never_match))[:8],
    }
    return f"v{ALGO_VERSION}:{_sha1(doc)[:16]}"


def _censor_indexes(
    entries: Sequence[WordEntry],
) -> tuple[
    dict[tuple[str, int], tuple[CensorCandidate, ...]],
    dict[str, tuple[CensorCandidate, ...]],
]:
    by_letter_len: dict[tuple[str, int], list[CensorCandidate]] = {}
    by_letter: dict[str, list[CensorCandidate]] = {}
    for entry in entries:
        if entry.is_phrase:
            continue
        for form in entry.forms:
            folded = fold(form)
            if not folded:
                continue
            cand = CensorCandidate(folded, entry.canonical, entry.category)
            by_letter_len.setdefault((folded[0], len(folded)), []).append(cand)
            by_letter.setdefault(folded[0], []).append(cand)
    return (
        {k: tuple(v) for k, v in by_letter_len.items()},
        {k: tuple(v) for k, v in by_letter.items()},
    )


def build_matcher(
    entries: Sequence[WordEntry] | None = None,
    profile: ProfileSpec | None = None,
    whitelist: Sequence[WhitelistRule] = (),
    never_match: NeverMatch | frozenset[str] | None = None,
) -> Matcher:
    """Compile a :class:`Matcher` for ``profile``."""
    profile = profile or ProfileSpec()
    all_entries = tuple(entries) if entries is not None else load_builtin_entries()
    if never_match is None:
        blocked = load_never_match().never_match
    elif isinstance(never_match, NeverMatch):
        blocked = never_match.never_match
    else:
        blocked = never_match

    active = select_entries(all_entries, profile)
    pattern = compile_pattern(active)

    ordered = sorted(active, key=lambda e: (-e.longest_form, e.canonical))
    by_group = {f"w{i}": e for i, e in enumerate(ordered)}
    by_canonical = {e.canonical: e for e in active}

    suppress_all: set[str] = set()
    suppress_ctx: dict[str, list[str]] = {}
    for rule in whitelist:
        if rule.context_text:
            suppress_ctx.setdefault(rule.canonical, []).append(fold(rule.context_text))
        else:
            suppress_all.add(rule.canonical)

    by_letter_len, by_letter = _censor_indexes(active)
    return Matcher(
        pattern=pattern,
        entries=active,
        profile=profile,
        profile_hash=profile_hash(profile, active, whitelist, blocked),
        never_match=blocked,
        _by_group=by_group,
        _by_canonical=by_canonical,
        _suppress_all=frozenset(suppress_all),
        _suppress_ctx={k: tuple(v) for k, v in suppress_ctx.items()},
        by_letter_len=by_letter_len,
        by_letter=by_letter,
    )


def mask_text(matched: str, mask_char: str = "*") -> str:
    """Replace every non-separator character with ``mask_char``.

    Separators are preserved so ``"God damn it"`` redacts to ``"*** **** it"``
    rather than ``"******** it"``: line length and wrapping are unchanged and the
    result still reads as two words.
    """
    return "".join(mask_char if ch not in " \t\xa0-'’" else ch for ch in matched)


def default_profile(categories: Iterable[str] | None = None) -> ProfileSpec:
    cats = frozenset(categories) if categories is not None else DEFAULT_CATEGORIES
    unknown = cats - set(WORD_CATEGORIES)
    if unknown:
        raise ValueError(f"unknown categories: {sorted(unknown)}")
    return ProfileSpec(categories=cats)
