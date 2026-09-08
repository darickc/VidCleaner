"""opt-in backfill: media_items.skip_backfill and titles.backfill_from

PLAN.md §2's Selection decision changes for series: enabling one no longer enqueues
the files it already had. Two columns, because they answer different questions.

`media_items.skip_backfill` is what the Title page's checkboxes write, and its default
of 0 is what keeps a series enabled before this migration behaving exactly as it did.
It cannot do the job alone: `sync_all` pulls items only for *enabled* titles, so a
series being enabled for the first time usually has **no** `media_items` rows yet and
there is nothing to mark. `titles.backfill_from` is the watermark that lets the sync
mark them correctly whenever it does create them -- anything the arr dates before the
moment the user enabled the series is pre-existing.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `batch_alter_table` for the same reason 0003 uses it: SQLite's ALTER cannot add
    # a NOT NULL column without a default, and the batch recreates the table when it
    # has to. The server default backfills existing rows with 0 -- deliberately, see
    # the module docstring.
    with op.batch_alter_table("media_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "skip_backfill",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
    # NULL = grandfathered: a series enabled before this migration keeps backfilling.
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.add_column(sa.Column("backfill_from", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("titles", schema=None) as batch_op:
        batch_op.drop_column("backfill_from")
    with op.batch_alter_table("media_items", schema=None) as batch_op:
        batch_op.drop_column("skip_backfill")
