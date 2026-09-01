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

from pydantic import Field
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

    config_dir: Path = Field(default_factory=lambda: _dir_default("/config", "config"))
    media_dir: Path = Field(default_factory=lambda: _dir_default("/media", "media"))
    backups_dir: Path = Field(default_factory=lambda: _dir_default("/backups", "backups"))
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
    def runs_api(self) -> bool:
        return self.role in ("api", "all")

    @property
    def runs_worker(self) -> bool:
        return self.role in ("worker", "all")

    def ensure_dirs(self) -> None:
        """Create the directories we own. /media is the library and is never created."""
        for path in (self.config_dir, self.backups_dir, self.work_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
