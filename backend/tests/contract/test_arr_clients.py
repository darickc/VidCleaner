"""Sonarr and Radarr over `httpx.MockTransport` (PLAN.md §12's contract tier)."""

from __future__ import annotations

import logging

import httpx
import pytest

from tests.support import fake_arr
from vidcleaner.integrations.base import (
    IntegrationAuthError,
    IntegrationError,
    IntegrationNotConfigured,
    IntegrationUnavailable,
)
from vidcleaner.integrations.models import poster_url
from vidcleaner.integrations.radarr import RadarrClient
from vidcleaner.integrations.sonarr import SonarrClient

KEY = "sonarr-api-key-do-not-log"


@pytest.fixture
def slept():
    return []


def sonarr_client(service: fake_arr.FakeService, slept: list[float], **kw) -> SonarrClient:
    return SonarrClient(
        "http://sonarr:8989",
        KEY,
        transport=service.transport(),
        sleep=slept.append,
        jitter=lambda: 0.0,
        **kw,
    )


def radarr_client(service: fake_arr.FakeService, slept: list[float], **kw) -> RadarrClient:
    return RadarrClient(
        "http://radarr:7878",
        KEY,
        transport=service.transport(),
        sleep=slept.append,
        jitter=lambda: 0.0,
        **kw,
    )


# ------------------------------------------------------------------- reading


def test_test_reports_the_version(slept) -> None:
    service = fake_arr.sonarr()
    result = sonarr_client(service, slept).test()
    assert result.ok and result.version == "4.0.10.2544"
    assert result.detail == "Sonarr"
    assert service.paths("GET") == ["/api/v3/system/status"]


def test_series_are_parsed_with_their_ids_and_poster(slept) -> None:
    series = sonarr_client(fake_arr.sonarr(), slept).list_series()
    assert [s.id for s in series] == [42, 43]
    assert (series[0].title, series[0].tvdb_id, series[0].path) == (
        "Pluribus",
        445598,
        "/tv/Pluribus",
    )
    assert poster_url(series[0].images) == "https://artworks.example/poster.jpg"
    assert poster_url(series[1].images) is None


def test_episode_files_are_listed_by_series(slept) -> None:
    service = fake_arr.sonarr()
    files = sonarr_client(service, slept).list_episode_files(42)
    assert [f.id for f in files] == [501, 502]
    assert files[0].path.startswith("/tv/Pluribus/")
    assert "seriesId=42" in service.last("GET", "/api/v3/episodefile").query


def test_episodes_without_a_file_are_visible(slept) -> None:
    episodes = sonarr_client(fake_arr.sonarr(), slept).list_episodes(42)
    assert [e.episode_file_id for e in episodes] == [501, 502, 0]
    assert episodes[2].has_file is False


def test_a_missing_file_is_none_not_an_exception(slept) -> None:
    """§6's "path vanished" path calls this precisely because it may be gone."""
    service = fake_arr.sonarr()
    client = sonarr_client(service, slept)
    assert client.get_episode_file(501) is not None
    assert client.get_episode_file(999) is None


def test_movies_carry_their_file(slept) -> None:
    movies = radarr_client(fake_arr.radarr(), slept).list_movies()
    assert [m.id for m in movies] == [7, 8]
    assert movies[0].movie_file is not None
    assert movies[0].movie_file.path.endswith("Arrival (2016) Bluray-1080p.mkv")
    assert movies[0].library_path == "/movies/Arrival (2016)"
    assert movies[1].has_file is False


# ------------------------------------------------------------------ commands


def test_a_rescan_posts_sonarrs_exact_command_body(slept) -> None:
    service = fake_arr.sonarr()
    status = sonarr_client(service, slept).rescan_series(42)
    assert service.last("POST", "/api/v3/command").body == {
        "name": "RescanSeries",
        "seriesId": 42,
    }
    assert (status.id, status.name) == (9, "RescanSeries")


def test_a_rescan_posts_radarrs_exact_command_body(slept) -> None:
    service = fake_arr.radarr()
    radarr_client(service, slept).rescan_movie(7)
    assert service.last("POST", "/api/v3/command").body == {"name": "RescanMovie", "movieId": 7}


def test_a_command_can_be_polled(slept) -> None:
    assert sonarr_client(fake_arr.sonarr(), slept).command_status(9).status == "completed"


def test_creating_the_webhook_sends_the_url_and_the_secret_header(slept) -> None:
    """§8: the setup page can create the notification on a click."""
    service = fake_arr.sonarr()
    sonarr_client(service, slept).create_webhook_notification(
        "http://vidcleaner:8585/api/webhooks/sonarr", "s3cret"
    )
    body = service.last("POST", "/api/v3/notification").body
    fields = {f["name"]: f["value"] for f in body["fields"]}
    assert fields["url"] == "http://vidcleaner:8585/api/webhooks/sonarr"
    assert fields["headers"] == "X-VidCleaner-Token=s3cret"
    assert body["implementation"] == "Webhook" and body["onDownload"] is True


def test_creating_a_notification_is_never_retried(slept) -> None:
    """A duplicate notification would double every future event -- worse than a
    visible failure the user can retry deliberately."""
    service = fake_arr.sonarr()
    service.fail[("POST", "/api/v3/notification")] = 503
    with pytest.raises(IntegrationUnavailable):
        sonarr_client(service, slept).create_webhook_notification("http://x/y", "t")
    assert service.count("POST", "/api/v3/notification") == 1
    assert slept == []


# --------------------------------------------------------------- retry policy


def test_a_bad_key_is_not_retried(slept) -> None:
    service = fake_arr.sonarr()
    service.fail[("GET", "/api/v3/series")] = 401
    with pytest.raises(IntegrationAuthError, match="check the API key"):
        sonarr_client(service, slept).list_series()
    assert service.count("GET", "/api/v3/series") == 1
    assert slept == []


def test_a_server_error_is_retried_with_backoff(slept) -> None:
    service = fake_arr.sonarr()
    service.fail[("GET", "/api/v3/series")] = 503
    with pytest.raises(IntegrationUnavailable, match="503"):
        sonarr_client(service, slept).list_series()
    assert service.count("GET", "/api/v3/series") == 3
    assert slept == [0.5, 1.0]


def test_a_transient_error_succeeds_on_the_retry(slept) -> None:
    service = fake_arr.sonarr()
    service.fail[("GET", "/api/v3/series")] = 503
    service.fail_times = 1
    assert len(sonarr_client(service, slept).list_series()) == 2
    assert service.count("GET", "/api/v3/series") == 2
    assert slept == [0.5]


def test_a_read_timeout_is_retried(slept) -> None:
    service = fake_arr.sonarr()
    service.raise_timeout.add(("GET", "/api/v3/series"))
    service.timeout_times = 1
    assert len(sonarr_client(service, slept).list_series()) == 2


def test_a_client_error_that_is_not_auth_is_terminal(slept) -> None:
    service = fake_arr.sonarr()
    service.fail[("GET", "/api/v3/series")] = 400
    with pytest.raises(IntegrationError) as caught:
        sonarr_client(service, slept).list_series()
    assert not isinstance(caught.value, IntegrationUnavailable)
    assert service.count("GET", "/api/v3/series") == 1


def test_a_rescan_is_retried_because_it_is_idempotent(slept) -> None:
    service = fake_arr.sonarr()
    service.fail[("POST", "/api/v3/command")] = 503
    service.fail_times = 1
    assert sonarr_client(service, slept).rescan_series(42).id == 9
    assert service.count("POST", "/api/v3/command") == 2


# ------------------------------------------------------------------ base urls


@pytest.mark.parametrize(
    "base",
    ["http://sonarr:8989", "http://sonarr:8989/", "sonarr:8989", "https://sonarr.example"],
)
def test_base_urls_resolve_to_the_same_api_path(base: str, slept) -> None:
    service = fake_arr.sonarr()
    client = SonarrClient(base, KEY, transport=service.transport(), sleep=slept.append)
    client.test()
    assert service.paths("GET") == ["/api/v3/system/status"]


def test_a_sub_path_base_url_is_preserved(slept) -> None:
    """Reverse proxies commonly serve Sonarr at http://host/sonarr."""
    service = fake_arr.FakeService(
        routes={("GET", "/sonarr/api/v3/system/status"): {"appName": "Sonarr", "version": "4"}}
    )
    client = SonarrClient("http://host/sonarr/", KEY, transport=service.transport())
    assert client.test().ok


def test_an_unconfigured_client_never_opens_a_socket() -> None:
    for url, key in (("", KEY), ("http://sonarr:8989", "")):
        with pytest.raises(IntegrationNotConfigured):
            SonarrClient(url, key)


def test_a_non_http_url_is_refused() -> None:
    with pytest.raises(IntegrationNotConfigured, match="scheme"):
        SonarrClient("ftp://sonarr", KEY)


# ------------------------------------------------------------------- secrecy


def test_the_api_key_is_sent_on_every_request(slept) -> None:
    service = fake_arr.sonarr()
    client = sonarr_client(service, slept)
    client.test()
    client.list_series()
    assert all(c.headers.get("x-api-key") == KEY for c in service.calls)


def test_the_api_key_never_reaches_a_log_record_or_an_exception(slept, caplog) -> None:
    """It travels through every request, so it is the one secret with that exposure."""
    service = fake_arr.sonarr()
    service.fail[("GET", "/api/v3/series")] = 503
    with caplog.at_level(logging.DEBUG), pytest.raises(IntegrationError) as caught:
        sonarr_client(service, slept).list_series()

    assert KEY not in str(caught.value)
    assert KEY not in repr(caught.value)
    for record in caplog.records:
        assert KEY not in record.getMessage()
        assert KEY not in str(getattr(record, "__dict__", {}))


def test_the_client_is_a_context_manager(slept) -> None:
    service = fake_arr.sonarr()
    with sonarr_client(service, slept) as client:
        assert client.test().ok


def test_a_204_is_not_an_error() -> None:
    service = fake_arr.FakeService(routes={("GET", "/api/v3/system/status"): None})
    client = SonarrClient("http://sonarr", KEY, transport=service.transport())
    result = client.test()
    assert result.ok and result.version is None


def test_a_connection_error_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = SonarrClient(
        "http://sonarr", KEY, transport=httpx.MockTransport(refuse), sleep=lambda _: None
    )
    with pytest.raises(IntegrationUnavailable, match="could not reach"):
        client.list_series()


def test_a_bad_key_makes_test_a_red_row_not_an_exception(slept) -> None:
    """§9.6's Test button. Never a 500: an unreachable or misconfigured service is
    a reason on screen."""
    service = fake_arr.sonarr()
    service.fail[("GET", "/api/v3/system/status")] = 401
    result = sonarr_client(service, slept).test()
    assert result.ok is False and "API key" in result.detail


def test_an_unreachable_arr_makes_test_a_red_row(slept) -> None:
    service = fake_arr.sonarr()
    service.raise_timeout.add(("GET", "/api/v3/system/status"))
    result = sonarr_client(service, slept).test()
    assert result.ok is False and "timed out" in result.detail
