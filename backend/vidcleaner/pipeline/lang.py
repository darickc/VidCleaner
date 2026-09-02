"""ISO 639 language code handling.

Matroska tags use ISO 639-2/B (``eng``, ``fre``, ``ger``), faster-whisper wants
639-1 (``en``, ``fr``, ``de``), and real files carry all of it plus regional
suffixes (``pt-BR``) and the placeholder ``und``. Only the codes likely to appear
in a media library are mapped; anything unknown passes through unchanged so it
still round-trips into the output file.
"""

from __future__ import annotations

__all__ = [
    "ENGLISH",
    "UNDETERMINED",
    "is_english",
    "matches",
    "normalize_tag",
    "to_iso639_1",
]

UNDETERMINED = "und"
ENGLISH = "eng"

#: 639-2/B (and a few 639-2/T variants) -> 639-1.
_TO_ISO1 = {
    "eng": "en",
    "spa": "es",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "ita": "it",
    "por": "pt",
    "rus": "ru",
    "jpn": "ja",
    "kor": "ko",
    "chi": "zh",
    "zho": "zh",
    "dut": "nl",
    "nld": "nl",
    "swe": "sv",
    "nor": "no",
    "dan": "da",
    "fin": "fi",
    "pol": "pl",
    "cze": "cs",
    "ces": "cs",
    "hun": "hu",
    "gre": "el",
    "ell": "el",
    "tur": "tr",
    "ara": "ar",
    "heb": "he",
    "hin": "hi",
    "tha": "th",
    "vie": "vi",
    "ind": "id",
    "may": "ms",
    "msa": "ms",
    "ukr": "uk",
    "bul": "bg",
    "rum": "ro",
    "ron": "ro",
    "slo": "sk",
    "slk": "sk",
    "slv": "sl",
    "hrv": "hr",
    "srp": "sr",
    "est": "et",
    "lav": "lv",
    "lit": "lt",
    "cat": "ca",
    "tam": "ta",
    "tel": "te",
    "ben": "bn",
    "mar": "mr",
    "urd": "ur",
    "fas": "fa",
    "per": "fa",
    "isl": "is",
    "ice": "is",
    "gle": "ga",
    "wel": "cy",
    "cym": "cy",
    "eus": "eu",
    "baq": "eu",
    "glg": "gl",
    "afr": "af",
    "swa": "sw",
    "mal": "ml",
    "kan": "kn",
}

#: 639-1 -> canonical 639-2/B, for tagging output streams.
_TO_ISO2 = {}
for _iso2, _iso1 in _TO_ISO1.items():
    _TO_ISO2.setdefault(_iso1, _iso2)


def normalize_tag(tag: str | None) -> str | None:
    """Lowercase and strip a regional suffix (``pt-BR`` -> ``pt``).

    ``None`` and the empty string stay ``None``: an *absent* language tag is
    meaningful and must not be turned into ``und``, because the render mirrors
    absence rather than asserting a language it does not know.
    """
    if not tag:
        return None
    cleaned = tag.strip().lower().replace("_", "-")
    if not cleaned:
        return None
    return cleaned.split("-", 1)[0]


def to_iso639_1(tag: str | None) -> str | None:
    """The code faster-whisper expects, or ``None`` if unknown/undetermined."""
    code = normalize_tag(tag)
    if code is None or code == UNDETERMINED:
        return None
    if len(code) == 2:
        return code
    return _TO_ISO1.get(code)


def to_iso639_2(tag: str | None) -> str | None:
    code = normalize_tag(tag)
    if code is None:
        return None
    if len(code) == 3:
        return code
    return _TO_ISO2.get(code)


def matches(tag: str | None, preferred: str | None) -> bool:
    """Do two tags name the same language, across 639-1/639-2 spellings?"""
    if not tag or not preferred:
        return False
    left, right = normalize_tag(tag), normalize_tag(preferred)
    if left == right:
        return True
    return to_iso639_1(left) is not None and to_iso639_1(left) == to_iso639_1(right)


def is_english(tag: str | None) -> bool:
    return matches(tag, ENGLISH)
