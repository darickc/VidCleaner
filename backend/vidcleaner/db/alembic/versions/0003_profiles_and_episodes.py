"""whitelist override mode, the episode join table, and a real movie constraint

Three additions M1--M4 deferred by name in the Decision Log (PLAN.md §14).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: A CLI run's rows hang off the sentinel title with `season`/`episode` NULL and no
#: `arr_file_id`, so the movie constraint below must exclude them or the second
#: `vidcleaner clean` of a session would violate it. Written as a literal for the
#: same reason 0002's is: a migration describes the schema as it was here.
_REAL_MOVIE_ROW = "season IS NULL AND episode IS NULL AND arr_file_id IS NOT NULL"


def upgrade() -> None:
    # (a) §7's "global -> title -> item" reads as an override chain, but the schema
    # had no negative form, so `load_whitelist` could only union suppressions and a
    # narrower scope could never restore a word.
    with op.batch_alter_table("whitelist", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "mode",
                sa.String(length=16),
                nullable=False,
                server_default="suppress",
            )
        )

    # (b) §5 cannot represent a multi-episode file: one `episodeFile` maps to several
    # `episodes[]` under scalar season/episode. `media_items.season`/`episode` keep
    # holding the *lowest* pair -- M3's stable natural key, which the unique
    # constraint and every sync path depend on -- and this table carries the rest.
    op.create_table(
        "media_item_episodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("episode_title", sa.Text(), nullable=True),
        sa.Column("arr_episode_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_item_id", "season", "episode", name="uq_media_item_episodes_item_s_e"
        ),
    )
    with op.batch_alter_table("media_item_episodes", schema=None) as batch_op:
        batch_op.create_index(
            "ix_media_item_episodes_media_item_id", ["media_item_id"], unique=False
        )

    # (c) SQLite treats NULLs as distinct, so `uq_media_items_title_s_e` does not
    # constrain movie rows at all and repeated syncs could accumulate duplicates.
    # M3 guarded that in code (a movie resolves by `title_id` alone); this makes the
    # database agree -- for arr-backed rows only, per `_REAL_MOVIE_ROW`.
    op.create_index(
        "ux_media_items_one_movie_per_title",
        "media_items",
        ["title_id"],
        unique=True,
        sqlite_where=sa.text(_REAL_MOVIE_ROW),
    )


def downgrade() -> None:
    op.drop_index("ux_media_items_one_movie_per_title", table_name="media_items")
    with op.batch_alter_table("media_item_episodes", schema=None) as batch_op:
        batch_op.drop_index("ix_media_item_episodes_media_item_id")
    op.drop_table("media_item_episodes")
    with op.batch_alter_table("whitelist", schema=None) as batch_op:
        batch_op.drop_column("mode")
