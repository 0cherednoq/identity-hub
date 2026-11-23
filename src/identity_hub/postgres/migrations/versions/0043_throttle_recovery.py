"""Backfill GCRA recovery independently of audit retention.

Revision ID: 0043
Revises: 0042
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    return op.get_context().version_table_schema


def upgrade() -> None:
    schema = _schema()
    recovery = op.create_table(
        "ih_throttle_recovery",
        sa.Column("identity_id", sa.Uuid(), primary_key=True),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_throttle_recovery_identity"),
            ondelete="CASCADE",
        ),
        schema=schema,
    )
    history = sa.table(
        "ih_lease_history",
        sa.column("identity_id"),
        sa.column("space_id"),
        sa.column("released_at"),
        sa.column("end_reason", sa.String()),
        schema=schema,
    )
    identities = sa.table("ih_identities", sa.column("id"), sa.column("space_id"), schema=schema)
    # Older audit records use the same reason for voluntary and forced yield.
    last_end = (
        sa.select(history.c.identity_id, history.c.space_id, sa.func.max(history.c.released_at))
        .join(
            identities,
            (history.c.identity_id == identities.c.id) & (history.c.space_id == identities.c.space_id),
        )
        .where(
            history.c.released_at.is_not(None),
            history.c.end_reason.in_(["expired", "lost", "revoked", "max_hold", "yielded"]),
        )
        .group_by(history.c.identity_id, history.c.space_id)
    )
    op.execute(sa.insert(recovery).from_select(["identity_id", "space_id", "ended_at"], last_end))


def downgrade() -> None:
    op.drop_table("ih_throttle_recovery", schema=_schema())
