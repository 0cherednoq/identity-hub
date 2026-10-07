"""Persistent GCRA throttle state.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "ih_throttle_state",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("tat", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_throttle_state_identity"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", "name", name=op.f("pk_ih_throttle_state")),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("ih_throttle_state", schema=_schema())
