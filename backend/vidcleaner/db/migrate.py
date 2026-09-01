"""Programmatic ``alembic upgrade head``.

The container entrypoint migrates before starting either process; this is the safety
net for dev runs and for a worker that starts against a fresh volume. Alembic is
idempotent, so running it in both places costs one query when already at head.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from vidcleaner.config import Settings, get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
SCRIPT_LOCATION = Path(__file__).resolve().parent / "alembic"


def alembic_config(settings: Settings | None = None) -> Config:
    settings = settings or get_settings()
    config = Config(str(ALEMBIC_INI) if ALEMBIC_INI.exists() else None)
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def upgrade_to_head(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")
