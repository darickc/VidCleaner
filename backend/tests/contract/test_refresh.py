"""The `refresh` stage: arr rescan + Jellyfin path notification (§6 step 9).

Refresh is non-fatal by design -- `swap` has already committed, so the library file
is correct and a failed rescan costs a stale entry until the hourly sync. Every test
here that injects a failure asserts the stage still *succeeds*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import fake_arr
from vidcleaner.config import Settings
from vidcleaner.integrations import Integrations
from vidcleaner.integrations.jellyfin import JellyfinClient
from vidcleaner.integrations.pathmap import PathMap, PathRule
from vidcleaner.integrations.radarr import RadarrClient
from vidcleaner.integrations.sonarr import SonarrClient
from vidcleaner.pipeline import refresh as refresh_stage
from vidcleaner.pipeline.artifacts import JobTarget, ProfileSnapshot, SwapResult
from vidcleaner.pipeline.stages import build_context, build_spec, run_stage
from vidcleaner.settings_store import AppSettings

JELLY_MAP = PathMap.from_rules("jellyfin", [PathRule("/data/tv", "/media/tv")])


@pytest.fixture
def world(settings: Settings, tmp_path: Path):
    class World:
        def __init__(self) -> None:
            self.sonarr_svc = fake_arr.sonarr()
            self.radarr_svc = fake_arr.radarr()
            self.jelly_svc = fake_arr.jellyfin()

        def clients(self, *, sonarr=True, radarr=False, jellyfin=True) -> Integrations:
            return Integrations(
                sonarr=SonarrClient(
                    "http://sonarr",
                    "k",
                    transport=self.sonarr_svc.transport(),
                    sleep=lambda _: None,
                )
                if sonarr
                else None,
                radarr=RadarrClient(
                    "http://radarr",
                    "k",
                    transport=self.radarr_svc.transport(),
                    sleep=lambda _: None,
                )
                if radarr
                else None,
                jellyfin=JellyfinClient(
                    "http://jellyfin",
                    "k",
                    transport=self.jelly_svc.transport(),
                    sleep=lambda _: None,
                )
                if jellyfin
                else None,
                jellyfin_map=JELLY_MAP,
            )

        def run(self, *, integrations, old_path: str | None = None, target=None):
            source = tmp_path / "S01E01.mkv"
            source.write_bytes(b"x")
            spec = build_spec(
                source,
                profile=ProfileSnapshot(profile_hash="v1:test"),
                settings=AppSettings(),
                target=target
                if target is not None
                else JobTarget(
                    media_item_id=1, title_id=1, kind="episode", arr_app="sonarr", arr_id=42
                ),
            )
            ctx = build_context(spec, deploy=settings)
            ctx.integrations = integrations
            SwapResult(
                original_path=str(source),
                final_path="/media/tv/Show/S01E01.mkv",
                backup_path="/backups/tv/Show/S01E01.mkv",
                old_path=old_path,
            ).write(ctx.ws.swap_json)
            run_stage(ctx, "refresh")
            return ctx

    return World()


def test_the_arr_is_rescanned_and_jellyfin_told_once(world) -> None:
    ctx = world.run(integrations=world.clients())
    result = refresh_stage.load(ctx.ws)

    assert world.sonarr_svc.last("POST", "/api/v3/command").body == {
        "name": "RescanSeries",
        "seriesId": 42,
    }
    assert result.arr == "sonarr" and result.arr_command_id == 9
    assert world.jelly_svc.count("POST", "/Library/Media/Updated") == 1
    assert result.warnings == [] and result.skipped == []


def test_the_path_sent_to_jellyfin_is_mapped(world) -> None:
    """We store local paths; Jellyfin needs its own."""
    world.run(integrations=world.clients())
    body = world.jelly_svc.last("POST", "/Library/Media/Updated").body
    assert body == {"Updates": [{"Path": "/data/tv/Show/S01E01.mkv", "UpdateType": "Modified"}]}


def test_an_mp4_swap_deletes_the_old_name_and_creates_the_new_one(world) -> None:
    """§6 remembers the `Deleted` for the old `.mp4` and leaves the new name as
    `Modified` -- but Jellyfin has never seen the `.mkv`, so it is `Created`."""
    world.run(integrations=world.clients(), old_path="/media/tv/Show/S01E01.mp4")
    body = world.jelly_svc.last("POST", "/Library/Media/Updated").body
    assert body == {
        "Updates": [
            {"Path": "/data/tv/Show/S01E01.mkv", "UpdateType": "Created"},
            {"Path": "/data/tv/Show/S01E01.mp4", "UpdateType": "Deleted"},
        ]
    }


def test_a_movie_rescans_radarr(world) -> None:
    world.run(
        integrations=world.clients(sonarr=False, radarr=True),
        target=JobTarget(media_item_id=1, title_id=1, kind="movie", arr_app="radarr", arr_id=7),
    )
    assert world.radarr_svc.last("POST", "/api/v3/command").body == {
        "name": "RescanMovie",
        "movieId": 7,
    }


def test_a_failed_rescan_warns_and_the_stage_still_succeeds(world) -> None:
    world.sonarr_svc.fail[("POST", "/api/v3/command")] = 503
    ctx = world.run(integrations=world.clients())
    result = refresh_stage.load(ctx.ws)

    assert ctx.ws.is_done("refresh"), "the library is already correct"
    assert any("rescan failed" in w for w in result.warnings)
    # Jellyfin was still told, because the two are independent.
    assert world.jelly_svc.count("POST", "/Library/Media/Updated") == 1


def test_a_failed_jellyfin_refresh_warns_and_the_stage_still_succeeds(world) -> None:
    world.jelly_svc.fail[("POST", "/Library/Media/Updated")] = 500
    ctx = world.run(integrations=world.clients())
    result = refresh_stage.load(ctx.ws)
    assert ctx.ws.is_done("refresh")
    assert any("jellyfin" in w for w in result.warnings)


def test_an_unconfigured_integration_is_skipped_not_warned(world) -> None:
    """Nothing went wrong: a library with no Jellyfin is a fine deployment."""
    ctx = world.run(integrations=world.clients(jellyfin=False))
    result = refresh_stage.load(ctx.ws)
    assert result.skipped == ["jellyfin_not_configured"]
    assert result.warnings == []


def test_no_integrations_at_all_is_skipped(world) -> None:
    ctx = world.run(integrations=None)
    result = refresh_stage.load(ctx.ws)
    assert result.skipped == ["no_integrations"]
    assert result.warnings == []


def test_a_cli_job_with_no_arr_target_is_skipped(world) -> None:
    """The M1 sentinel has no arr id, so there is nothing to rescan."""
    ctx = world.run(
        integrations=world.clients(),
        target=JobTarget(media_item_id=1, title_id=1, kind="movie", arr_app=None),
    )
    result = refresh_stage.load(ctx.ws)
    assert "no_arr_target" in result.skipped
    assert world.sonarr_svc.count("POST", "/api/v3/command") == 0
    # Jellyfin is still worth telling: the file on disk did change.
    assert world.jelly_svc.count("POST", "/Library/Media/Updated") == 1


def test_running_refresh_twice_is_harmless(world) -> None:
    """What replaces stage purity here: a rescan and a path notification are both
    idempotent, which is what makes a resumed job safe."""
    ctx = world.run(integrations=world.clients())
    run_stage(ctx, "refresh", force=True)
    assert world.sonarr_svc.count("POST", "/api/v3/command") == 2
    assert refresh_stage.load(ctx.ws).warnings == []
