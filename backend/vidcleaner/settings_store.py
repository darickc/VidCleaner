"""Operational settings: the ``settings`` table, one row per key (PLAN.md §5).

These are the values the user edits on the Settings page. Deployment config (paths,
role, port) lives in :mod:`vidcleaner.config` instead and is not stored here.

Secret fields are encrypted at rest and never leave the API in plaintext: reads return
``MASK`` when a secret is set, and writing ``MASK`` back is a no-op, so a round-trip of
the settings form cannot wipe a key the user never saw.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from vidcleaner.crypto import get_secret_box
from vidcleaner.db.models import Setting

MASK = "***"


class AppSettings(BaseModel):
    """Defaults mirror the decisions in PLAN.md §2/§3/§7."""

    model_config = {"extra": "forbid"}

    # --- integrations (§8) ---
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    webhook_token: str = ""

    # --- speech to text (§3) ---
    stt_windowed_model: str = "large-v3-turbo"
    stt_full_model: str = "medium"
    stt_drift_model: str = "small"
    stt_full_max_hours: float = Field(
        default=3.0,
        ge=0.0,
        description=(
            "Refuse to *promote* a job to a full-file pass above this runtime "
            "(PLAN.md §13). 0 = no limit. An explicit --stt-mode full ignores it."
        ),
    )
    cpu_threads: int = Field(default=0, ge=0, description="0 = auto (cores - 2)")
    beam_size: int = Field(default=2, ge=1, le=5)
    vad_filter: bool = False
    """Silero VAD. **Only affects full/audit passes**: faster-whisper documents
    that "vad_filter will be ignored if clip_timestamps is used", and windowed
    passes always use clip_timestamps -- so this has never applied to the default
    mode. Off by default because it is actively harmful where it *does* apply:
    measured on the eval set, full-mode recall is 0.61 without it and 0.11 with
    it, at identical precision. See docs/eval.md and the Decision Log."""
    initial_prompt_hint: bool = True
    mute_censored_tokens: bool = True
    preferred_language: str = "eng"

    # --- subtitle drift (§6 step 3) ---
    drift_check: bool = True
    """Measure the subtitle offset before choosing STT windows. ~10 s per job."""
    drift_window_pad_s: float = Field(
        default=6.0,
        ge=0.0,
        le=30.0,
        description=(
            "Cue padding used when drift says the timing is unreliable. §6 step 3's "
            "+-6 s; the reliable case keeps subtitles.WINDOW_PAD_S."
        ),
    )

    # --- detection & muting (§7) ---
    pad_pre_ms: int = Field(default=80, ge=0, le=2000)
    pad_post_ms: int = Field(default=120, ge=0, le=2000)
    merge_gap_ms: int = Field(default=250, ge=0, le=5000)
    fade_edges_ms: int = Field(default=0, ge=0, le=100)

    # --- output (§3 codec policy) ---
    clean_track_lossless: bool = False
    extra_eac3_downmix: bool = False
    redact_subtitles: bool = True

    # --- library swap (§6 step 8) ---
    allow_cross_device_backup: bool = False
    """Permit a backup that cannot be made by ``rename``.

    §10 already *assumes* `/backups` is on the same filesystem as the library ("so
    swaps are same-filesystem renames"). Where it is not, backing up means copy +
    verify + **unlink the original**, and CLAUDE.md reserves unlinking library files
    to nobody at all. Off by default, with an actionable error, rather than silently
    degrading to a delete."""

    # --- scheduling & retention (§6) ---
    audit_pass: Literal["off", "idle", "always"] = "idle"
    mapping_check_delay_s: float = Field(
        default=90.0,
        ge=0.0,
        description=(
            "§6 step 9's 90 s, as a scheduling parameter rather than a sleep: the "
            "sync pass re-checks the arr's path for items cleaned longer ago than "
            "this. A sleep inside the stage would idle the worker per job."
        ),
    )
    render_parallel: int = Field(default=1, ge=1, le=4)
    backup_retention_days: int = Field(default=30, ge=0)


SECRET_FIELDS: frozenset[str] = frozenset(
    {
        "sonarr_api_key",
        "radarr_api_key",
        "jellyfin_api_key",
        "webhook_token",
    }
)


def load_settings(session: Session) -> AppSettings:
    """Stored rows layered over the defaults. Unknown/legacy keys are ignored."""
    rows = session.execute(select(Setting)).scalars().all()
    box = get_secret_box()
    values: dict[str, Any] = {}
    for row in rows:
        if row.key not in AppSettings.model_fields:
            continue
        try:
            value = json.loads(row.value_json)
        except ValueError:
            continue
        if row.key in SECRET_FIELDS and isinstance(value, str):
            value = box.decrypt(value)
        values[row.key] = value
    return AppSettings.model_validate(values)


def save_settings(session: Session, patch: dict[str, Any]) -> AppSettings:
    """Apply a partial update and return the full effective settings.

    Raises ``pydantic.ValidationError`` for unknown keys or bad values; nothing is
    written in that case.
    """
    current = load_settings(session)
    # A masked secret means "unchanged" — drop it before validating.
    patch = {
        key: value for key, value in patch.items() if not (key in SECRET_FIELDS and value == MASK)
    }
    merged = AppSettings.model_validate({**current.model_dump(), **patch})

    box = get_secret_box()
    existing = {row.key: row for row in session.execute(select(Setting)).scalars()}
    for key in patch:
        value = getattr(merged, key)
        if key in SECRET_FIELDS and isinstance(value, str):
            value = box.encrypt(value)
        payload = json.dumps(value)
        if key in existing:
            existing[key].value_json = payload
        else:
            session.add(Setting(key=key, value_json=payload))
    session.flush()
    return merged


def masked(settings: AppSettings) -> dict[str, Any]:
    """Serialisation for the API: secrets become ``***`` when set, ``""`` when not."""
    data = settings.model_dump()
    for key in SECRET_FIELDS:
        data[key] = MASK if data.get(key) else ""
    return data
