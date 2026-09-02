"""Prefix rewrites between our paths and an app's (PLAN.md §2, §8).

§5 gives ``path_mappings(app, from_prefix, to_prefix)`` and never says which side is
ours, while §8 needs both directions (an arr hands us paths; we hand Jellyfin paths).
Pinned here: **``from_prefix`` is the app's path and ``to_prefix`` is ours.** One
stored convention, two accessors, and the Settings page labels the columns
"Sonarr path" -> "VidCleaner path".

Two rules §5 does not state and both of which matter:

* **Longest prefix wins**, evaluated once. No chaining.
* **Matching is component-aware.** A naive ``startswith`` makes a rule for
  ``/media/tv`` rewrite ``/media/tvshows`` -- a silent wrong-path bug whose best case
  is a refused swap.

Everything here is string manipulation, never ``pathlib``: a Sonarr running on Windows
reports ``C:\\media\\TV\\...``, and ``Path`` on Linux would mangle it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.db.models import PathMapping
from vidcleaner.logging import get_logger

__all__ = ["PathMap", "PathRule", "load_path_map"]

log = get_logger(__name__)


def _norm(prefix: str) -> str:
    """Strip trailing separators. Both kinds, because the app may be on Windows."""
    return prefix.rstrip("/\\")


def _starts_with_component(path: str, prefix: str) -> bool:
    if not prefix:
        return False
    if path == prefix:
        return True
    return path.startswith(prefix + "/") or path.startswith(prefix + "\\")


@dataclass(frozen=True, slots=True)
class PathRule:
    from_prefix: str
    """The app's path."""
    to_prefix: str
    """Ours."""


@dataclass(frozen=True, slots=True)
class PathMap:
    app: str
    rules: tuple[PathRule, ...] = field(default_factory=tuple)
    """Sorted longest ``from_prefix`` first at construction, so the first match wins."""

    @classmethod
    def from_rules(cls, app: str, rules: Iterable[PathRule]) -> PathMap:
        cleaned = [PathRule(_norm(r.from_prefix), _norm(r.to_prefix)) for r in rules]
        cleaned = [r for r in cleaned if r.from_prefix and r.to_prefix]
        duplicates = _duplicates(cleaned)
        if duplicates:
            raise ValueError(f"{app} path mappings have duplicate prefixes: {duplicates}")
        return cls(app, tuple(sorted(cleaned, key=lambda r: -len(r.from_prefix))))

    @classmethod
    def from_rows(cls, app: str, rows: Iterable[PathMapping]) -> PathMap:
        return cls.from_rules(app, (PathRule(r.from_prefix, r.to_prefix) for r in rows))

    def __bool__(self) -> bool:
        """False means identity, which is the default and the common case (§2)."""
        return bool(self.rules)

    def to_local(self, remote: str) -> str:
        """An app's path -> ours. Unmapped input is returned unchanged."""
        return self._apply(remote, forward=True)

    def to_remote(self, local: str) -> str:
        """Our path -> an app's."""
        return self._apply(local, forward=False)

    def _apply(self, value: str, *, forward: bool) -> str:
        if not value:
            return value
        rules = (
            self.rules if forward else tuple(sorted(self.rules, key=lambda r: -len(r.to_prefix)))
        )
        for rule in rules:
            source = rule.from_prefix if forward else rule.to_prefix
            target = rule.to_prefix if forward else rule.from_prefix
            if _starts_with_component(value, source):
                return target + value[len(source) :]
        if self.rules:
            log.debug("pathmap.unmapped", app=self.app, path=value, forward=forward)
        return value


def _duplicates(rules: list[PathRule]) -> list[str]:
    """A duplicate ``to_prefix`` is as bad as a duplicate ``from_prefix``: it makes
    ``to_remote`` ambiguous, and §5 constrains neither."""
    out: list[str] = []
    for attr in ("from_prefix", "to_prefix"):
        seen: set[str] = set()
        for rule in rules:
            value = getattr(rule, attr)
            if value in seen:
                out.append(value)
            seen.add(value)
    return sorted(set(out))


def load_path_map(session: Session, app: str) -> PathMap:
    rows = session.scalars(select(PathMapping).where(PathMapping.app == app)).all()
    try:
        return PathMap.from_rows(app, rows)
    except ValueError as exc:
        # A broken mapping table must not take the integration down; identity is
        # wrong but recoverable, and the Settings page can show the problem.
        log.error("pathmap.invalid", app=app, error=str(exc))
        return PathMap(app)
