"""`httpx.MockTransport` handlers standing in for Sonarr, Radarr and Jellyfin.

§12 asks for "respx-mocked" clients. `httpx.MockTransport` does the same job with no
new dependency and nothing pinned to httpx internals: the handler is a plain
function, so a test can route by (method, path), return a committed JSON fixture,
**and** assert on the recorded call sequence. It also matches the injection idiom the
project already uses (`FFmpegRunner`, `ScriptedTranscriber`, `FsOps`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "arr"

__all__ = ["Call", "FakeService", "fixture", "sonarr", "radarr", "jellyfin"]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@dataclass
class Call:
    method: str
    path: str
    query: str = ""
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def where(self) -> tuple[str, str]:
        return self.method, self.path


@dataclass
class FakeService:
    """Route table plus a call log, with programmable failures.

    ``routes`` maps ``(method, path)`` to either a payload or a callable taking the
    request. ``fail`` maps the same key to a status code, applied a bounded number of
    times so a test can prove a retry succeeded on the second attempt.
    """

    routes: dict[tuple[str, str], Any] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    fail: dict[tuple[str, str], int] = field(default_factory=dict)
    fail_times: int | None = None
    raise_timeout: set[tuple[str, str]] = field(default_factory=set)
    timeout_times: int | None = None

    def route(self, method: str, path: str, payload: Any) -> FakeService:
        self.routes[(method.upper(), path)] = payload
        return self

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append(
            Call(
                method=request.method,
                path=request.url.path,
                query=str(request.url.query.decode()),
                body=_body(request),
                headers=dict(request.headers),
            )
        )

        if key in self.raise_timeout:
            if self.timeout_times is not None:
                self.timeout_times -= 1
                if self.timeout_times <= 0:
                    self.raise_timeout.discard(key)
            raise httpx.ReadTimeout("timed out", request=request)

        if key in self.fail:
            status = self.fail[key]
            if self.fail_times is not None:
                self.fail_times -= 1
                if self.fail_times <= 0:
                    del self.fail[key]
            return httpx.Response(status, json={"message": "nope"})

        if key in self.routes:
            payload = self.routes[key]
            if callable(payload):
                return payload(request)
            if payload is None:
                return httpx.Response(204)
            return httpx.Response(200, json=payload)

        return httpx.Response(404, json={"message": f"no route for {key}"})

    # -------------------------------------------------------------- queries

    def paths(self, method: str | None = None) -> list[str]:
        return [c.path for c in self.calls if method is None or c.method == method]

    def count(self, method: str, path: str) -> int:
        return sum(1 for c in self.calls if c.where == (method, path))

    def last(self, method: str, path: str) -> Call:
        return [c for c in self.calls if c.where == (method, path)][-1]


def _body(request: httpx.Request) -> Any:
    if not request.content:
        return None
    try:
        return json.loads(request.content)
    except ValueError:
        return request.content.decode(errors="replace")


def sonarr(**overrides: Any) -> FakeService:
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/api/v3/system/status"): {"appName": "Sonarr", "version": "4.0.10.2544"},
        ("GET", "/api/v3/series"): fixture("sonarr_series"),
        ("GET", "/api/v3/series/42"): fixture("sonarr_series")[0],
        ("GET", "/api/v3/episode"): fixture("sonarr_episodes"),
        ("GET", "/api/v3/episodefile"): fixture("sonarr_episodefiles"),
        ("GET", "/api/v3/episodefile/501"): fixture("sonarr_episodefiles")[0],
        ("POST", "/api/v3/command"): {"id": 9, "name": "RescanSeries", "status": "queued"},
        ("GET", "/api/v3/command/9"): {"id": 9, "name": "RescanSeries", "status": "completed"},
        ("GET", "/api/v3/notification"): [],
        ("POST", "/api/v3/notification"): {"id": 3, "name": "VidCleaner"},
    }
    routes.update({(k[0].upper(), k[1]): v for k, v in overrides.items()})  # type: ignore[index]
    return FakeService(routes=routes)


def radarr(**overrides: Any) -> FakeService:
    routes: dict[tuple[str, str], Any] = {
        ("GET", "/api/v3/system/status"): {"appName": "Radarr", "version": "5.14.0.9383"},
        ("GET", "/api/v3/movie"): fixture("radarr_movies"),
        ("GET", "/api/v3/movie/7"): fixture("radarr_movies")[0],
        ("GET", "/api/v3/moviefile"): fixture("radarr_moviefiles"),
        ("GET", "/api/v3/moviefile/901"): fixture("radarr_moviefiles")[0],
        ("POST", "/api/v3/command"): {"id": 11, "name": "RescanMovie", "status": "queued"},
        ("GET", "/api/v3/notification"): [],
        ("POST", "/api/v3/notification"): {"id": 4, "name": "VidCleaner"},
    }
    routes.update({(k[0].upper(), k[1]): v for k, v in overrides.items()})  # type: ignore[index]
    return FakeService(routes=routes)


def jellyfin() -> FakeService:
    return FakeService(
        routes={
            ("GET", "/System/Info"): {"ServerName": "unraid", "Version": "10.10.3"},
            ("POST", "/Library/Media/Updated"): None,  # 204
            ("POST", "/Library/Series/Updated"): None,
            ("POST", "/Library/Movies/Updated"): None,
        }
    )
