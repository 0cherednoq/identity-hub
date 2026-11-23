"""Durable batch login retry schedule.

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    return op.get_context().version_table_schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_credentials",
        sa.Column("login_version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        schema=schema,
    )
    op.create_table(
        "ih_login_retry",
        sa.Column("identity_id", sa.Uuid(), primary_key=True),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("credentials", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_login_retry_identity"),
            ondelete="CASCADE",
        ),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("ih_login_retry", schema=_schema())
    op.drop_column("ih_credentials", "login_version", schema=_schema())
