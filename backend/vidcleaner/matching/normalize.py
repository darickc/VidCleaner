"""Token normalization, censored-token detection, and offset mapping.

PLAN.md §7 says normalization should "lowercase, strip punctuation/apostrophes,
collapse whitespace", but it also requires detecting *censored* tokens (``f***``,
``s___``, ``f-ing``) -- and those need the punctuation that normalization would
throw away. The tension is resolved by making normalization **lossless first**
and producing three levels, with classification happening between level 1 and
level 2 so nothing is discarded before it has been inspected::

    raw     exactly as faster-whisper or pysubs2 produced it; never mutated
      |  strip_wrappers()  -- remove OUTER wrapper punctuation only
      +-> classify()       -- Token.kind + CensorInfo, on the STILL-punctuated core
      |  normalize()       -- NFKC, casefold, curly->straight, keep - and '
      +-> norm             -- what the compiled pattern runs on
      |  fold()            -- additionally drop ' and -
      +-> fold             -- never_match lookup, rapidfuzz, whitelist context

§7's "strip punctuation/apostrophes" therefore applies to the *fold* level, not
to the level the pattern sees. That is why the compiler's word-internal
separator has to tolerate ``'`` and ``-``: ``norm`` still contains them.

Deviation from the original design sketch: ``.`` is **not** treated as a mask
character. Doing so would classify the extremely common subtitle ellipsis
("That...", "I...") as a censored token. Masking is recognised only from
``* _ # @ $`` and runs of two or more hyphens.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "CensorInfo",
    "CensorCandidate",
    "JoinedText",
    "Token",
    "TokenKind",
    "censored_candidates",
    "detect_censored",
    "fold",
    "join_tokens",
    "normalize",
    "span_to_tokens",
    "strip_wrappers",
    "tokenize",
]

TokenKind = Literal["word", "censored", "empty"]

#: Characters stripped from both ends of a raw token. Deliberately excludes the
#: mask characters (``* _ # @ $``), ``-`` (needed for the ``f--k`` rule) and
#: ``'`` (word-final in ``fuckin'``; ``fold`` removes it later anyway).
_WRAPPERS = '"“”«»‹›()[]{}<>,.!?:;…—–~|/\\¡¿+=&^%'

_MASK_CHARS = frozenset("*_#@$")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

# `f***`, `sh*t`, `a$$`, `s___` -- a leading letter plus at least one mask char.
_MASKED_RE = re.compile(r"^[A-Za-z][A-Za-z\*_#@$]{1,13}$")
# `f--k`, `s---` -- a leading letter then a run of two or more hyphens.
_DBLDASH_RE = re.compile(r"^([A-Za-z])-{2,}([A-Za-z]*)$")
# `f-ing`, `f-in` -- one letter, one hyphen, a short lowercase tail.
_HYPHEN_RE = re.compile(r"^([A-Za-z])-([a-z]{1,6})$")

_MAX_MASK_LEN = 14


@dataclass(frozen=True, slots=True)
class CensorInfo:
    """Evidence that a token is a self-censored spelling of some word."""

    style: Literal["masked", "hyphen"]
    first_letter: str
    length: int
    """Visible length of the token, mask characters included."""
    revealed: tuple[tuple[int, str], ...]
    """``(position, letter)`` pairs that survived the masking, e.g. ``sh*t``."""
    suffix: str
    """The tail of a hyphen-style token (``ing`` for ``f-ing``); ``""`` otherwise."""


@dataclass(frozen=True, slots=True)
class CensorCandidate:
    form: str
    canonical: str
    category: str


@dataclass(frozen=True, slots=True)
class Token:
    """One transcript or cue word at all three normalization levels."""

    index: int
    raw: str
    norm: str
    fold: str
    kind: TokenKind
    censor: CensorInfo | None = None
    start_s: float | None = None
    end_s: float | None = None
    prob: float | None = None

    @property
    def is_censored(self) -> bool:
        return self.kind == "censored"


def strip_wrappers(raw: str) -> str:
    """Remove outer wrapper punctuation, preserving mask characters."""
    return raw.strip().strip(_WRAPPERS).strip()


def normalize(core: str) -> str:
    """NFKC, casefold, curly apostrophes to straight, whitespace collapsed.

    Internal ``-`` and ``'`` survive: the compiled pattern relies on them.
    """
    text = unicodedata.normalize("NFKC", core).casefold()
    text = text.replace("’", "'").replace("ʼ", "'").replace("‘", "'")
    text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
    return " ".join(text.split())


def fold(text: str) -> str:
    """``normalize`` plus removal of apostrophes and hyphens.

    This is the level used for ``never_match`` lookups, ``rapidfuzz`` comparison
    and whitelist context matching, so that ``fuckin'`` and ``fuckin`` compare
    equal and ``god-damn`` matches a ``god damn`` context.
    """
    return normalize(text).replace("'", "").replace("-", "")


def _has_letter(text: str) -> bool:
    return _LETTER_RE.search(text) is not None


def detect_censored(core: str, never_match: Iterable[str] = ()) -> CensorInfo | None:
    """Classify ``core`` as a self-censored token, or return ``None``.

    Two distinct styles, because one rule cannot serve both:

    ``masked``
        Contains ``* _ # @ $``, or a run of 2+ hyphens after a leading letter.
        No English word contains these, so this fires on its own evidence.

    ``hyphen``
        A single leading letter, one hyphen, a short tail (``f-ing``). This is a
        *shape* heuristic and it collides with ``x-ray``, ``e-mail``,
        ``t-shirt``, ``a-list``, ``u-turn`` -- which is precisely what
        ``never_match.yaml`` exists to exclude. Callers must additionally
        require subtitle evidence before acting on it.
    """
    if not core or len(core) > _MAX_MASK_LEN or not _has_letter(core):
        return None

    blocked = never_match if isinstance(never_match, (set, frozenset)) else frozenset(never_match)
    if fold(core) in blocked:
        return None

    dbl = _DBLDASH_RE.match(core)
    if dbl is not None:
        lead, tail = dbl.group(1), dbl.group(2)
        revealed = ((0, lead.lower()),) + tuple(
            (len(core) - len(tail) + i, ch.lower()) for i, ch in enumerate(tail)
        )
        return CensorInfo("masked", lead.lower(), len(core), revealed, "")

    if _MASK_CHARS.intersection(core):
        if _MASKED_RE.match(core) is None:
            return None
        revealed = tuple((i, ch.lower()) for i, ch in enumerate(core) if ch.isalpha())
        return CensorInfo("masked", core[0].lower(), len(core), revealed, "")

    hyp = _HYPHEN_RE.match(core)
    if hyp is not None:
        lead, tail = hyp.group(1).lower(), hyp.group(2)
        return CensorInfo("hyphen", lead, len(core), ((0, lead),), tail)

    return None


def censored_candidates(
    info: CensorInfo,
    by_letter_len: Mapping[tuple[str, int], tuple[CensorCandidate, ...]],
    by_letter: Mapping[str, tuple[CensorCandidate, ...]],
) -> tuple[CensorCandidate, ...]:
    """Resolve a censored token to the enabled forms it could stand for.

    ``masked`` uses §7's first-letter + length +/-1 rule, then narrows by the
    letters that survived the masking -- free, and strictly better: ``s***``
    stays ambiguous while ``sh*t`` resolves to ``shit`` alone.

    ``hyphen`` must NOT use the length rule (``f-ing`` is 5 characters,
    ``fucking`` is 7); it matches on first letter plus the tail as a suffix.
    """
    if info.style == "hyphen":
        return tuple(
            c
            for c in by_letter.get(info.first_letter, ())
            if info.suffix and c.form.endswith(info.suffix)
        )

    out: list[CensorCandidate] = []
    for length in (info.length - 1, info.length, info.length + 1):
        for cand in by_letter_len.get((info.first_letter, length), ()):
            if all(pos < len(cand.form) and cand.form[pos] == ch for pos, ch in info.revealed):
                out.append(cand)
    seen: set[str] = set()
    unique: list[CensorCandidate] = []
    for cand in out:
        if cand.form not in seen:
            seen.add(cand.form)
            unique.append(cand)
    return tuple(unique)


def tokenize(
    raw: str,
    *,
    index: int = 0,
    never_match: Iterable[str] = (),
    start_s: float | None = None,
    end_s: float | None = None,
    prob: float | None = None,
) -> Token:
    """Build a :class:`Token` from one raw word, at all three levels."""
    core = strip_wrappers(raw)
    censor = detect_censored(core, never_match)
    if censor is not None:
        kind: TokenKind = "censored"
        norm = normalize(core)
    elif _has_letter(core):
        kind = "word"
        norm = normalize(core)
    else:
        kind = "empty"
        norm = ""
    return Token(
        index=index,
        raw=raw,
        norm=norm,
        fold=fold(norm),
        kind=kind,
        censor=censor,
        start_s=start_s,
        end_s=end_s,
        prob=prob,
    )


@dataclass(frozen=True, slots=True)
class JoinedText:
    """``norm`` values of a token sequence joined for whole-transcript matching.

    Invariants, asserted in the tests:

    * ``text[starts[i]:ends[i]] == tokens[i].norm`` for every ``i``
    * ``starts`` is monotonically non-decreasing
    * a zero-width (punctuation-only) token has ``starts[i] == ends[i]`` and
      emits no separator, so token indices never shift
    """

    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]


def join_tokens(tokens: Sequence[Token]) -> JoinedText:
    """Join ``norm`` values with exactly one space, recording offsets.

    Exactly one space, never a newline: a newline would let the phrase separator
    bridge a sentence boundary (the bug behind PLAN.md §7's ``[\\s\\-']+``), and
    two spaces would make phrase matching behave differently here than in the
    per-cue windowed path. Keeping them identical is what makes the M2 audit
    pass comparable to the windowed pass.
    """
    parts: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    cursor = 0
    for tok in tokens:
        if not tok.norm:
            starts.append(cursor)
            ends.append(cursor)
            continue
        if parts:
            parts.append(" ")
            cursor += 1
        starts.append(cursor)
        parts.append(tok.norm)
        cursor += len(tok.norm)
        ends.append(cursor)
    return JoinedText("".join(parts), tuple(starts), tuple(ends))


def span_to_tokens(joined: JoinedText, start: int, end: int) -> tuple[int, int]:
    """Map a ``[start, end)`` character span back to inclusive token indices."""
    n = len(joined.starts)
    if n == 0:
        raise ValueError("cannot map a span onto an empty token sequence")

    first = max(0, bisect_right(joined.starts, start) - 1)
    if joined.ends[first] <= start:
        first += 1
    while first < n and joined.ends[first] == joined.starts[first]:
        first += 1
    if first >= n:
        raise ValueError(f"span start {start} maps past the last token")

    last = min(n - 1, max(first, bisect_left(joined.starts, end) - 1))
    while last > first and joined.ends[last] == joined.starts[last]:
        last -= 1
    return first, last
