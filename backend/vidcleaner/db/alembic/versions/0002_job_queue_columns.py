"""job queue columns: retry_at, force, and one live job per item

Everything the M3 worker needs that PLAN.md §5's ``jobs`` table does not have.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept as a literal rather than imported from ``db.constants``: a migration must
#: describe the schema as it was at this revision, and would otherwise change
#: meaning the next time someone edits ``TERMINAL_STATES``.
_ACTIVE_JOB = "state NOT IN ('done', 'failed', 'already_clean', 'stale', 'cancelled')"


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        # §6 promises "transient API failures retry with backoff", which has nowhere
        # to live: without a scheduling column a requeued job is claimable at once
        # and the worker hot-loops on it.
        batch_op.add_column(sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True))
        # §4 requires "unless forced" and §9.3 offers "reprocess all"; deriving it
        # from `trigger` would conflate two independent things.
        batch_op.add_column(
            sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.create_index("ix_jobs_retry_at", ["retry_at"], unique=False)

    # A partial unique index, so the *database* rejects a second live job for one
    # media item. The api (webhooks, backfill) and the worker (audit passes) enqueue
    # from separate processes, so no SELECT-then-INSERT check can be atomic.
    op.create_index(
        "ux_jobs_one_active_per_item",
        "jobs",
        ["media_item_id"],
        unique=True,
        sqlite_where=sa.text(_ACTIVE_JOB),
    )


def downgrade() -> None:
    op.drop_index("ux_jobs_one_active_per_item", table_name="jobs")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_jobs_retry_at")
        batch_op.drop_column("force")
        batch_op.drop_column("retry_at")
