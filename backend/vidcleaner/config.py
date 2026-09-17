"""Deployment configuration.

Two distinct layers exist in VidCleaner and they are deliberately kept apart:

* **Deployment config** (this module) — where things live and how the processes run:
  paths, role, port, log level. Sourced from environment variables, then
  ``<config_dir>/settings.json``, then defaults. Fixed for the life of the process.
* **Operational settings** (:mod:`vidcleaner.settings_store`) — what the user edits in
  the UI: integration URLs/keys, STT models, padding, codec policy, retention. Stored
  in the ``settings`` table (PLAN.md §5) and changeable at runtime.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

ENV_PREFIX = "VIDCLEANER_"

# Repo-local fallbacks so the app runs on a dev machine that has no container mounts.
_DEV_ROOT = Path(__file__).resolve().parents[2] / ".local"


def _dir_default(container_path: str, dev_name: str) -> Path:
    """Use the container mount when it exists, else a repo-local dev directory."""
    path = Path(container_path)
    return path if path.is_dir() else _DEV_ROOT / dev_name


#: Where originals are kept. Visible on purpose: this is the one directory in the
#: install that silently accumulates full-size video files, and a dot-prefixed name
#: makes it the one directory nobody sees while tidying the share.
BACKUPS_DIRNAME = "VidCleaner-Backups"
#: What it was called before, and what the relocator looks for.
LEGACY_BACKUPS_DIRNAME = ".vidcleaner-backups"


def _config_dir_from_env() -> Path:
    """Resolve config_dir early, since it locates the settings.json source itself."""
    raw = os.environ.get(f"{ENV_PREFIX}CONFIG_DIR")
    return Path(raw) if raw else _dir_default("/config", "config")


def _static_dir_default() -> Path:
    """The image copies the built SPA to /app/static; dev serves frontend/dist."""
    packaged = Path("/app/static")
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


class JsonFileSettingsSource(PydanticBaseSettingsSource):
    """Reads ``<config_dir>/settings.json``. Missing or malformed file -> no values."""

    def __call__(self) -> dict[str, Any]:
        path = _config_dir_from_env() / "settings.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_field_value(self, field, field_name):  # pragma: no cover - unused hook
        raise NotImplementedError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX, extra="ignore")

    role: Literal["api", "worker", "all"] = "all"
    host: str = "0.0.0.0"
    port: int = 8585
    log_level: str = "INFO"
    sync_interval_minutes: float = 60.0
    """§8's "hourly". Deployment config rather than an operational setting because it
    is read once, when the api process starts its periodic task."""

    config_dir: Path = Field(default_factory=lambda: _dir_default("/config", "config"))
    media_dir: Path = Field(default_factory=lambda: _dir_default("/media", "media"))
    backups_dir: Path = Field(
        default_factory=lambda data: Path(data["media_dir"]) / BACKUPS_DIRNAME
    )
    """A subdirectory *inside* the media mount, never a mount of its own.

    `rename(2)` refuses to cross mount points even when both sides are the same
    filesystem (§14, 2026-09-03), so a separate `/backups` volume could never be the
    rename the swap needs -- and the entrypoint used to `mkdir /backups` regardless,
    which made that broken path the silent default for anyone who cleared the
    environment variable. Deriving it from `media_dir` removes the trap instead of
    papering over it, and follows a `media_dir` set in `settings.json` as well as one
    set in the environment."""
    work_dir: Path = Field(default_factory=lambda: _dir_default("/work", "work"))
    static_dir: Path = Field(default_factory=_static_dir_default)

    # Overrides the SQLite file derived from config_dir. Mainly for tests.
    db_url: str | None = None
    # Run `alembic upgrade head` on process start. The container entrypoint also
    # migrates before starting anything, so this mainly smooths dev runs.
    auto_migrate: bool = True

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Precedence: explicit init args > environment > settings.json > defaults.
        return (init_settings, env_settings, JsonFileSettingsSource(settings_cls))

    @property
    def database_url(self) -> str:
        return self.db_url or f"sqlite+pysqlite:///{self.config_dir / 'vidcleaner.db'}"

    @property
    def snippets_dir(self) -> Path:
        """Review clips for the Item page (§6 step 10).

        Under ``/config`` rather than ``/work`` because ``/work`` is reclaimed a week
        after a job finishes and the detections it illustrates live in the database
        forever. Derived rather than configurable: it is small (a few MB per movie)
        and belongs with the database it is keyed to."""
        return self.config_dir / "snippets"

    @property
    def legacy_backups_dir(self) -> Path:
        """Where originals lived before the directory was given a visible name."""
        return self.media_dir / LEGACY_BACKUPS_DIRNAME

    @property
    def backups_dir_is_hidden(self) -> bool:
        """An install that pinned the old value keeps working; it just cannot be seen.

        Shipping a new default does not change a saved `VIDCLEANER_BACKUPS_DIR`, so
        this is what the startup log and the Backups page use to say so out loud.
        """
        return self.backups_dir.name.startswith(".")

    @property
    def runs_api(self) -> bool:
        return self.role in ("api", "all")

    @property
    def runs_worker(self) -> bool:
        return self.role in ("worker", "all")

    @model_validator(mode="after")
    def _retire_the_legacy_backups_default(self) -> Settings:
        """`<media>/.vidcleaner-backups` is the old default, not a choice.

        The image, `docker-compose.yml` and the unraid template all shipped that
        literal as `VIDCLEANER_BACKUPS_DIR`, and an installed container keeps the
        variable it was created with -- so for exactly the installs that need to move,
        the old path is pinned from outside and "relocate only when `backups_dir` is
        the new default" would never fire. Treating that one exact value as unset is
        what lets an upgrade actually happen. Any other override is honoured
        untouched, and this is deterministic, so it re-applies identically on every
        boot rather than mutating anything on disk.
        """
        if (
            self.backups_dir.name == LEGACY_BACKUPS_DIRNAME
            and self.backups_dir.parent == self.media_dir
        ):
            self.backups_dir = self.media_dir / BACKUPS_DIRNAME
        return self

    def ensure_dirs(self) -> None:
        """Create the directories we own. /media is the library and is never created."""
        for path in (self.config_dir, self.backups_dir, self.work_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
