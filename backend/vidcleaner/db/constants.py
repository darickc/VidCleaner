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

#: ``jobs.priority`` values per trigger. **Lower runs sooner** -- §4 claims with
#: ``ORDER BY priority, created_at``, so §8's "backfill priority below webhook jobs"
#: means a *higher* number. PLAN.md never states the direction and it is a classic
#: silent inversion; the column's default of 100 is the webhook value.
DEFAULT_PRIORITY: Final[dict[str, int]] = {
    "manual": 50,
    "reprocess": 50,
    "webhook": 100,
    "backfill": 200,
    "audit": 900,
}

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
    "cancelled",
)

#: States a job never leaves. §6's list plus ``cancelled``.
TERMINAL_STATES: Final = ("done", "failed", "already_clean", "stale", "cancelled")

#: A job is being worked on. Complement of ``queued`` and ``TERMINAL_STATES``; this is
#: what stale-heartbeat recovery scans.
RUNNING_STATES: Final = tuple(s for s in JOB_STATES if s not in ("queued", *TERMINAL_STATES))

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

#: Which ``jobs.state`` a job is in while a given stage runs. Kept here rather than in
#: the worker so M4's API can render it without importing the worker, and because the
#: names are asymmetric in both directions (stage ``transcribe`` -> state
#: ``transcribing``, stage ``detect`` -> state ``detecting``, but ``subtitles`` and
#: ``snippets`` are spelled the same).
STAGE_TO_STATE: Final[dict[str, str]] = {
    "probe": "probing",
    "extract": "extracting",
    "subtitles": "subtitles",
    "transcribe": "transcribing",
    "detect": "detecting",
    "render": "rendering",
    "verify": "verifying",
    "swap": "swapping",
    "refresh": "refreshing",
    "snippets": "snippets",
}


# jobs.stt_mode
STT_MODES: Final = ("windowed", "full", "audit")

# detections.source
DETECTION_SOURCES: Final = ("subtitle", "stt", "both")

# word categories (§2)
WORD_CATEGORIES: Final = ("strong", "mild", "religious", "slurs", "sexual")

# whitelist.scope
WHITELIST_SCOPES: Final = ("global", "title", "item")

# whitelist.mode -- `allow` is §7's missing negative form (see the model docstring).
WHITELIST_MODES: Final = ("suppress", "allow")

#: Narrowest wins. Used by `matching.compiler` to resolve a word two rules claim.
WHITELIST_SCOPE_RANK: Final = {"global": 0, "title": 1, "item": 2}

# backups.state
BACKUP_STATES: Final = ("kept", "purged", "restored", "orphaned")

# path_mappings.app / webhook_events.source
APPS: Final = ("sonarr", "radarr", "jellyfin")
