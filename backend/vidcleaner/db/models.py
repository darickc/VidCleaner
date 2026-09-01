"""SQLAlchemy 2.x models — the complete data model from PLAN.md §5.

The whole schema is defined up front (and created by the 0001_initial migration) even
though M0 only reads/writes ``settings``: it is fully specified in the plan, so landing
it once avoids migration churn through M1-M3.

Back-pointers that would create a circular foreign key in SQLite
(``media_items.last_job_id`` -> ``jobs.id``, whose own ``media_item_id`` points back)
are plain columns without a constraint.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Setting(Base):
    """Operational settings edited from the UI. Secret values are stored encrypted."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PathMapping(Base):
    """Prefix rewrites between our paths and an app's. Empty table = identity (§2)."""

    __tablename__ = "path_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app: Mapped[str] = mapped_column(String(16), nullable=False)
    from_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    to_prefix: Mapped[str] = mapped_column(Text, nullable=False)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    categories_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    extra_word_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    pad_pre_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=80)
    pad_post_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Title(Base):
    """A Sonarr series or Radarr movie."""

    __tablename__ = "titles"
    __table_args__ = (UniqueConstraint("kind", "arr_id", name="uq_titles_kind_arr_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    arr_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tvdb_id: Mapped[int | None] = mapped_column(Integer)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    imdb_id: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    poster_url: Mapped[str | None] = mapped_column(Text)
    arr_path: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[MediaItem]] = relationship(back_populates="title", cascade="all, delete")


class MediaItem(Base):
    """One movie file or episode file we may clean."""

    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("title_id", "season", "episode", name="uq_media_items_title_s_e"),
        Index("ix_media_items_path", "path"),
        Index("ix_media_items_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    arr_file_id: Mapped[int | None] = mapped_column(Integer)
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    episode_title: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="untracked")
    last_job_id: Mapped[str | None] = mapped_column(String(36))  # no FK: circular with jobs
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    title: Mapped[Title] = relationship(back_populates="items")


class Job(Base):
    """A unit of pipeline work. This table doubles as the queue (§4)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "state", "priority", "created_at"),
        Index("ix_jobs_media_item_id", "media_item_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"))
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    stage: Mapped[str | None] = mapped_column(String(24))
    progress_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    claimed_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    work_dir: Mapped[str | None] = mapped_column(Text)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))
    stt_mode: Mapped[str | None] = mapped_column(String(16))
    model_used: Mapped[str | None] = mapped_column(String(64))
    subtitle_source: Mapped[str | None] = mapped_column(Text)
    profile_snapshot_json: Mapped[str | None] = mapped_column(Text)
    timings_json: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobLog(Base):
    """Structured job events. Raw ffmpeg stderr goes to a per-job file, not here."""

    __tablename__ = "job_logs"
    __table_args__ = (Index("ix_job_logs_job_id_ts", "job_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    msg: Mapped[str] = mapped_column(Text, nullable=False)


class Detection(Base):
    """One matched word, its timing, and how it was found (§7)."""

    __tablename__ = "detections"
    __table_args__ = (
        Index("ix_detections_media_item_id", "media_item_id"),
        Index("ix_detections_job_word", "job_id", "word_canonical"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"))
    word_raw: Mapped[str] = mapped_column(Text, nullable=False)
    word_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    start_s: Mapped[float] = mapped_column(Float, nullable=False)
    end_s: Mapped[float] = mapped_column(Float, nullable=False)
    mute_start_s: Mapped[float] = mapped_column(Float, nullable=False)
    mute_end_s: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    whitelisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspicious: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    subtitle_cue_idx: Mapped[int | None] = mapped_column(Integer)
    snippet_path: Mapped[str | None] = mapped_column(Text)


class WordEntry(Base):
    """A canonical word/phrase and its explicit inflection table (§7)."""

    __tablename__ = "word_entries"
    __table_args__ = (UniqueConstraint("canonical", name="uq_word_entries_canonical"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    forms_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    is_phrase: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WhitelistEntry(Base):
    """False-positive suppression, scoped global -> title -> item."""

    __tablename__ = "whitelist"
    __table_args__ = (Index("ix_whitelist_scope", "scope", "scope_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[int | None] = mapped_column(Integer)
    canonical_word: Mapped[str] = mapped_column(Text, nullable=False)
    context_text: Mapped[str | None] = mapped_column(Text)


class Backup(Base):
    """The untouched original, kept until purged (§2 — swaps are always reversible)."""

    __tablename__ = "backups"
    __table_args__ = (Index("ix_backups_media_item_id", "media_item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(36))  # kept after the job is pruned
    media_item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"))
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    backup_path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int | None] = mapped_column(Integer)
    sha1_prefix: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="kept")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEvent(Base):
    """Raw Sonarr/Radarr webhook payloads, stored before any work is decided (§6.0)."""

    __tablename__ = "webhook_events"
    __table_args__ = (Index("ix_webhook_events_received_at", "received_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(48))
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    handled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    job_id: Mapped[str | None] = mapped_column(String(36))
    note: Mapped[str | None] = mapped_column(Text)
