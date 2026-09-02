"""Jellyfin 10.10 over `httpx.MockTransport`.

Every assertion here pins an exact wire detail, because Jellyfin fails *silently*
when the body shape is wrong: `/Library/Media/Updated` still answers 204.
"""

from __future__ import annotations

import pytest

from tests.support import fake_arr
from vidcleaner.integrations.base import IntegrationAuthError
from vidcleaner.integrations.jellyfin import JellyfinClient, MediaUpdate

KEY = "jellyfin-key"


def client(service: fake_arr.FakeService, **kw) -> JellyfinClient:
    return JellyfinClient(
        "http://jellyfin:8096", KEY, transport=service.transport(), sleep=lambda _: None, **kw
    )


def test_the_auth_header_is_jellyfins_exact_form() -> None:
    service = fake_arr.jellyfin()
    client(service).test()
    assert service.calls[0].headers["authorization"] == f'MediaBrowser Token="{KEY}"'


def test_test_uses_the_authenticated_endpoint() -> None:
    """`/System/Info/Public` answers without a key, so it would report success for
    a wrong one."""
    service = fake_arr.jellyfin()
    result = client(service).test()
    assert result.ok and result.version == "10.10.3" and result.detail == "unraid"
    assert service.paths("GET") == ["/System/Info"]


def test_a_bad_key_fails_the_test_without_raising() -> None:
    """A wrong key is the likeliest reason anyone clicks Test, so it must come back
    as a red row with a reason rather than a 500 and a stack trace."""
    service = fake_arr.jellyfin()
    service.fail[("GET", "/System/Info")] = 401
    result = client(service).test()
    assert result.ok is False
    assert "401" in result.detail and "API key" in result.detail
    assert result.version is None


def test_an_unreachable_server_fails_the_test_without_raising() -> None:
    service = fake_arr.jellyfin()
    service.fail[("GET", "/System/Info")] = 503
    result = client(service).test()
    assert result.ok is False and "503" in result.detail


def test_a_bad_key_still_raises_for_a_real_call() -> None:
    """Only `test()` swallows it: a refresh that silently did nothing would be worse."""
    service = fake_arr.jellyfin()
    service.fail[("POST", "/Library/Media/Updated")] = 403
    with pytest.raises(IntegrationAuthError):
        client(service).media_updated([MediaUpdate(path="/media/x.mkv")])


def test_media_updated_sends_the_documented_body() -> None:
    service = fake_arr.jellyfin()
    client(service).media_updated([MediaUpdate(path="/media/tv/x.mkv")])
    assert service.last("POST", "/Library/Media/Updated").body == {
        "Updates": [{"Path": "/media/tv/x.mkv", "UpdateType": "Modified"}]
    }


def test_every_changed_path_goes_in_one_call() -> None:
    """§3 measured a ~60 s debounce, so batching is both cheaper and no slower."""
    service = fake_arr.jellyfin()
    client(service).media_updated(
        [
            MediaUpdate(path="/media/tv/new.mkv", update_type="Created"),
            MediaUpdate(path="/media/tv/old.mp4", update_type="Deleted"),
        ]
    )
    assert service.count("POST", "/Library/Media/Updated") == 1
    assert service.last("POST", "/Library/Media/Updated").body == {
        "Updates": [
            {"Path": "/media/tv/new.mkv", "UpdateType": "Created"},
            {"Path": "/media/tv/old.mp4", "UpdateType": "Deleted"},
        ]
    }


def test_no_updates_means_no_call() -> None:
    service = fake_arr.jellyfin()
    client(service).media_updated([])
    assert service.calls == []


def test_a_204_is_success() -> None:
    service = fake_arr.jellyfin()
    client(service).media_updated([MediaUpdate(path="/media/x.mkv")])  # must not raise


def test_the_provider_id_refreshes_take_a_query_parameter() -> None:
    service = fake_arr.jellyfin()
    c = client(service)
    c.refresh_series(445598)
    c.refresh_movie(329865)
    assert "tvdbId=445598" in service.last("POST", "/Library/Series/Updated").query
    assert "tmdbId=329865" in service.last("POST", "/Library/Movies/Updated").query


def test_a_library_notification_is_retried() -> None:
    """Telling Jellyfin a path changed is idempotent, so a 503 is worth another go."""
    service = fake_arr.jellyfin()
    service.fail[("POST", "/Library/Media/Updated")] = 503
    service.fail_times = 1
    client(service).media_updated([MediaUpdate(path="/media/x.mkv")])
    assert service.count("POST", "/Library/Media/Updated") == 2
