"""Sonarr, Radarr and Jellyfin (PLAN.md §8).

Clients are **synchronous**. The worker is a sync process and ``refresh`` is a sync
stage; every existing FastAPI handler is a sync ``def`` over a sync ``Session``, which
FastAPI runs in a threadpool; and the webhook receivers make no outbound calls at all.
So async would buy nothing anywhere and would cost either an event loop in the worker
or a split session layer in the api. ``httpx.Client`` is documented thread-safe, so one
instance per client object is fine under the threadpool.

The **path invariant** that makes this layer checkable: *the database stores local
paths exclusively.* Mapping happens only here, at the boundary -- arr paths through
``PathMap.to_local`` on the way in, our paths through ``to_remote`` on the way to
Jellyfin -- and never on a ``/work`` or ``/backups`` path.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from vidcleaner.integrations.base import (
    IntegrationAuthError,
    IntegrationError,
    IntegrationNotConfigured,
    IntegrationUnavailable,
    TestResult,
)
from vidcleaner.integrations.jellyfin import JellyfinClient, MediaUpdate
from vidcleaner.integrations.pathmap import PathMap, load_path_map
from vidcleaner.integrations.radarr import RadarrClient
from vidcleaner.integrations.sonarr import SonarrClient
from vidcleaner.settings_store import AppSettings, load_settings

__all__ = [
    "Integrations",
    "IntegrationAuthError",
    "IntegrationError",
    "IntegrationNotConfigured",
    "IntegrationUnavailable",
    "JellyfinClient",
    "MediaUpdate",
    "PathMap",
    "RadarrClient",
    "SonarrClient",
    "TestResult",
    "for_settings",
    "load_path_map",
]


class Integrations:
    """The three clients plus their path maps, built once per job or request.

    Any of them may be ``None`` when its URL or key is unset; callers skip rather
    than fail, because a library with no Jellyfin is a perfectly good deployment.
    """

    __slots__ = ("jellyfin", "jellyfin_map", "radarr", "radarr_map", "sonarr", "sonarr_map")

    def __init__(
        self,
        *,
        sonarr: SonarrClient | None = None,
        radarr: RadarrClient | None = None,
        jellyfin: JellyfinClient | None = None,
        sonarr_map: PathMap | None = None,
        radarr_map: PathMap | None = None,
        jellyfin_map: PathMap | None = None,
    ) -> None:
        self.sonarr = sonarr
        self.radarr = radarr
        self.jellyfin = jellyfin
        self.sonarr_map = sonarr_map or PathMap("sonarr")
        self.radarr_map = radarr_map or PathMap("radarr")
        self.jellyfin_map = jellyfin_map or PathMap("jellyfin")

    def arr(self, app: str | None) -> SonarrClient | RadarrClient | None:
        return {"sonarr": self.sonarr, "radarr": self.radarr}.get(app or "")

    def map_for(self, app: str) -> PathMap:
        return {
            "sonarr": self.sonarr_map,
            "radarr": self.radarr_map,
            "jellyfin": self.jellyfin_map,
        }.get(app, PathMap(app))

    def close(self) -> None:
        for client in (self.sonarr, self.radarr, self.jellyfin):
            if client is not None:
                client.close()


def for_settings(
    settings: AppSettings, *, maps: dict[str, PathMap] | None = None, **client_kw
) -> Integrations:
    """Build whichever clients are configured. Unset URL or key -> ``None``."""
    maps = maps or {}

    def build(cls, url: str, key: str):
        return cls(url, key, **client_kw) if url and key else None

    return Integrations(
        sonarr=build(SonarrClient, settings.sonarr_url, settings.sonarr_api_key),
        radarr=build(RadarrClient, settings.radarr_url, settings.radarr_api_key),
        jellyfin=build(JellyfinClient, settings.jellyfin_url, settings.jellyfin_api_key),
        sonarr_map=maps.get("sonarr"),
        radarr_map=maps.get("radarr"),
        jellyfin_map=maps.get("jellyfin"),
    )


def from_database(session: Session, **client_kw) -> Integrations:
    """Clients built from the stored settings, with the stored path mappings."""
    return for_settings(
        load_settings(session),
        maps={app: load_path_map(session, app) for app in ("sonarr", "radarr", "jellyfin")},
        **client_kw,
    )
