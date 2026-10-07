"""Lease priority and cooperative yield request.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_leases", sa.Column("priority", sa.Integer(), nullable=False, server_default="0"), schema=schema
    )
    op.add_column(
        "ih_leases", sa.Column("yield_requested_at", sa.DateTime(timezone=True)), schema=schema
    )
    op.add_column("ih_leases", sa.Column("yield_requested_priority", sa.Integer()), schema=schema)
    op.add_column(
        "ih_lease_history",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_column("ih_lease_history", "priority", schema=schema)
    op.drop_column("ih_leases", "yield_requested_priority", schema=schema)
    op.drop_column("ih_leases", "yield_requested_at", schema=schema)
    op.drop_column("ih_leases", "priority", schema=schema)
