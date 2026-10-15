"""durable import acceptance queue and auth retryability (S7, D-317)

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "ih_acceptance",
        sa.Column("identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_ih_acceptance_attempts_nonnegative"),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name="fk_ih_acceptance_identity",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", name="pk_ih_acceptance"),
        schema=schema,
    )
    op.create_index(
        "ix_ih_acceptance_queue",
        "ih_acceptance",
        ["space_id", "created_at", "identity_id"],
        schema=schema,
    )
    op.add_column("ih_region_states", sa.Column("retryable", sa.Boolean(), nullable=True), schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_column("ih_region_states", "retryable", schema=schema)
    op.drop_index("ix_ih_acceptance_queue", table_name="ih_acceptance", schema=schema)
    op.drop_table("ih_acceptance", schema=schema)
