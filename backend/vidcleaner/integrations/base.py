"""HTTP plumbing shared by all three integrations.

Sonarr and Radarr are ~90% the same API -- ``/command``, ``/notification``,
``/system/status``, the ``X-Api-Key`` header, the retry policy -- so that lives in
:class:`ArrClient` here and the two named modules stay thin, which keeps §4's file
list honest.

Two constructor arguments exist for testing and are the same idiom as
``FFmpegRunner``/``ScriptedTranscriber``: ``transport`` (an ``httpx.MockTransport``
in tests, so §12's contract tier needs no new dependency) and ``sleep`` (so retry
tests take zero wall time).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from vidcleaner.logging import get_logger

__all__ = [
    "DEFAULT_TIMEOUT",
    "RETRYABLE_STATUS",
    "ArrClient",
    "BaseClient",
    "CommandStatus",
    "IntegrationAuthError",
    "IntegrationError",
    "IntegrationNotConfigured",
    "IntegrationUnavailable",
    "Notification",
    "TestResult",
]

log = get_logger(__name__)

#: A rescan on a large library can be slow to acknowledge; a connect that hangs
#: should not be.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class IntegrationError(RuntimeError):
    def __init__(
        self,
        app: str,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(f"{app}: {message}")
        self.app = app
        self.message = message
        self.status = status
        self.url = url
        self.retryable = retryable


class IntegrationNotConfigured(IntegrationError):
    """No URL or no key. Raised before any socket is opened."""


class IntegrationAuthError(IntegrationError):
    """401/403. Never retried -- the key is wrong and will stay wrong."""


class IntegrationUnavailable(IntegrationError):
    """Timeout, connection refused, or a 5xx. The only genuinely transient class."""


@dataclass(frozen=True, slots=True)
class TestResult:
    """What §9.6's Test button shows. Never an exception: an unreachable service is
    a red row with a reason, not a stack trace."""

    ok: bool
    app: str
    version: str | None = None
    detail: str = ""
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class CommandStatus:
    id: int | None
    name: str = ""
    status: str = ""


@dataclass(frozen=True, slots=True)
class Notification:
    id: int | None
    name: str = ""
    implementation: str = ""
    url: str = ""


class BaseClient:
    app: ClassVar[str] = "integration"
    #: Requests that are safe to repeat. A rescan command and a Jellyfin path
    #: notification both are; creating a notification is not, because a duplicate
    #: webhook is worse than a visible failure.
    IDEMPOTENT_POSTS: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        retries: int = 3,
        backoff_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not base_url or not api_key:
            raise IntegrationNotConfigured(
                self.app, "no URL or API key configured", url=base_url or None
            )
        parsed = urlsplit(base_url if "://" in base_url else f"http://{base_url}")
        if parsed.scheme not in ("http", "https"):
            raise IntegrationNotConfigured(self.app, f"unsupported URL scheme {parsed.scheme!r}")
        # A trailing slash is stripped, but a sub-path is preserved: reverse proxies
        # commonly serve Sonarr at http://host/sonarr.
        self.base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        self.api_key = api_key
        self.retries = max(1, retries)
        self.backoff_s = backoff_s
        self._sleep = sleep
        self._jitter = jitter
        self._client = httpx.Client(
            timeout=timeout, transport=transport, headers=self.auth_headers(), follow_redirects=True
        )

    # ------------------------------------------------------------- subclasses

    def auth_headers(self) -> dict[str, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def test(self) -> TestResult:  # pragma: no cover - overridden
        raise NotImplementedError

    # ----------------------------------------------------------------- request

    def url_for(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _retryable(self, method: str, path: str) -> bool:
        if method == "GET":
            return True
        return any(path.startswith(prefix) for prefix in self.IDEMPOTENT_POSTS)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: httpx.Timeout | float | None = None,
        allow_404: bool = False,
    ) -> Any:
        """One call, with retries. Returns parsed JSON, or ``None`` for an empty body.

        The API key is never included in an exception message or a log record. It
        travels through every request, so it is the one secret with that exposure.
        """
        url = self.url_for(path)
        attempts = self.retries if self._retryable(method.upper(), path) else 1
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.TimeoutException:
                last = IntegrationUnavailable(
                    self.app, f"timed out calling {path}", url=url, retryable=True
                )
                log.warning("integration.timeout", app=self.app, path=path, attempt=attempt)
            except httpx.HTTPError as exc:
                last = IntegrationUnavailable(
                    self.app, f"could not reach {path}: {exc}", url=url, retryable=True
                )
                log.warning("integration.unreachable", app=self.app, path=path, attempt=attempt)
            else:
                if response.status_code in (401, 403):
                    raise IntegrationAuthError(
                        self.app,
                        f"{response.status_code} from {path}; check the API key",
                        status=response.status_code,
                        url=url,
                    )
                if response.status_code == 404 and allow_404:
                    return None
                if response.status_code in RETRYABLE_STATUS:
                    last = IntegrationUnavailable(
                        self.app,
                        f"{response.status_code} from {path}",
                        status=response.status_code,
                        url=url,
                        retryable=True,
                    )
                    log.warning(
                        "integration.retryable",
                        app=self.app,
                        path=path,
                        status=response.status_code,
                        attempt=attempt,
                    )
                elif response.is_error:
                    raise IntegrationError(
                        self.app,
                        f"{response.status_code} from {path}: {response.text[:200]}",
                        status=response.status_code,
                        url=url,
                    )
                else:
                    return _decode(response)

            if attempt < attempts:
                self._sleep(self.backoff_s * (2 ** (attempt - 1)) * (1 + 0.1 * self._jitter()))

        assert last is not None
        raise last

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BaseClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _decode(response: httpx.Response) -> Any:
    if response.status_code == 204 or not response.content:
        # Jellyfin answers 204 to /Library/Media/Updated. That is success.
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


class ArrClient(BaseClient):
    """Everything Sonarr and Radarr share."""

    API: ClassVar[str] = "/api/v3"
    IDEMPOTENT_POSTS: ClassVar[tuple[str, ...]] = ("/api/v3/command",)
    RESCAN_COMMAND: ClassVar[str] = ""
    RESCAN_ID_FIELD: ClassVar[str] = ""

    def auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key, "Accept": "application/json"}

    def api(self, path: str) -> str:
        return f"{self.API}/{path.lstrip('/')}"

    def test(self) -> TestResult:
        """§3 names a Test endpoint only for Jellyfin. ``/system/status`` is the arr
        equivalent: it validates the key *and* returns a version, so §9.6 can show
        "Sonarr 4.0.x" rather than a bare green tick."""
        started = time.monotonic()
        try:
            payload = self.get(self.api("system/status")) or {}
        except IntegrationError as exc:
            return TestResult(False, self.app, detail=exc.message)
        return TestResult(
            True,
            self.app,
            version=str(payload.get("version") or "") or None,
            detail=str(payload.get("appName") or self.app),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def rescan(self, arr_id: int) -> CommandStatus:
        """§3: "Rescan after modifying a file"."""
        payload = self.post(
            self.api("command"),
            json={"name": self.RESCAN_COMMAND, self.RESCAN_ID_FIELD: arr_id},
        )
        return _command(payload)

    def command_status(self, command_id: int) -> CommandStatus:
        """So a future mapping check can poll a real state instead of guessing."""
        return _command(self.get(self.api(f"command/{command_id}"), allow_404=True))

    # --------------------------------------------------------- notifications

    def list_notifications(self) -> list[Notification]:
        rows = self.get(self.api("notification")) or []
        return [
            Notification(
                id=row.get("id"),
                name=str(row.get("name") or ""),
                implementation=str(row.get("implementation") or ""),
                url=_notification_url(row),
            )
            for row in rows
        ]

    def create_webhook_notification(
        self, url: str, token: str, *, name: str = "VidCleaner", header: str = "X-VidCleaner-Token"
    ) -> Notification:
        """§8's "can create the notification on user click".

        Never retried: a duplicate notification would double every future event,
        which is worse than a visible failure the user can retry deliberately.
        """
        body = {
            "name": name,
            "implementation": "Webhook",
            "implementationName": "Webhook",
            "configContract": "WebhookSettings",
            "onDownload": True,
            "onUpgrade": True,
            "onRename": True,
            "onSeriesDelete": True,
            "onMovieDelete": True,
            "onEpisodeFileDelete": True,
            "onMovieFileDelete": True,
            "fields": [
                {"name": "url", "value": url},
                {"name": "method", "value": 1},
                {"name": "headers", "value": f"{header}={token}"},
            ],
        }
        payload = self.post(self.api("notification"), json=body) or {}
        return Notification(
            id=payload.get("id"),
            name=str(payload.get("name") or name),
            implementation="Webhook",
            url=url,
        )


def _command(payload: Any) -> CommandStatus:
    if not isinstance(payload, dict):
        return CommandStatus(id=None)
    return CommandStatus(
        id=payload.get("id"),
        name=str(payload.get("name") or ""),
        status=str(payload.get("status") or ""),
    )


def _notification_url(row: dict[str, Any]) -> str:
    for field in row.get("fields") or []:
        if field.get("name") == "url":
            return str(field.get("value") or "")
    return ""
