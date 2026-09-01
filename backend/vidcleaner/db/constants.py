"""String literals used by the schema (PLAN.md §5/§6).

Stored as plain TEXT rather than SQL enums: SQLite has no native enum and adding a
value must never require a migration.
"""

from __future__ import annotations

from typing import Final

# titles.kind
TITLE_KINDS: Final = ("series", "movie")

# media_items.kind
ITEM_KINDS: Final = ("movie", "episode")

# media_items.status
ITEM_STATUSES: Final = (
    "untracked",
    "pending",
    "queued",
    "processing",
    "clean",
    "already_clean",
    "failed",
    "stale",
    "restored",
)

# jobs.trigger
JOB_TRIGGERS: Final = ("webhook", "backfill", "manual", "reprocess", "audit")

# jobs.state — the pipeline state machine (§6)
JOB_STATES: Final = (
    "queued",
    "probing",
    "extracting",
    "subtitles",
    "transcribing",
    "detecting",
    "rendering",
    "verifying",
    "swapping",
    "refreshing",
    "snippets",
    "done",
    "failed",
    "already_clean",
    "stale",
)

# The ordered stages a job passes through; also the <stage>.done marker names in /work.
JOB_STAGES: Final = (
    "probe",
    "extract",
    "subtitles",
    "transcribe",
    "detect",
    "render",
    "verify",
    "swap",
    "refresh",
    "snippets",
)

# jobs.stt_mode
STT_MODES: Final = ("windowed", "full", "audit")

# detections.source
DETECTION_SOURCES: Final = ("subtitle", "stt", "both")

# word categories (§2)
WORD_CATEGORIES: Final = ("strong", "mild", "religious", "slurs", "sexual")

# whitelist.scope
WHITELIST_SCOPES: Final = ("global", "title", "item")

# backups.state
BACKUP_STATES: Final = ("kept", "purged", "restored", "orphaned")

# path_mappings.app / webhook_events.source
APPS: Final = ("sonarr", "radarr", "jellyfin")
