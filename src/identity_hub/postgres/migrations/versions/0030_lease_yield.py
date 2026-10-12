"""Cooperative lease yield and short priority reservation.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column("ih_identities", sa.Column("yield_reserved_priority", sa.Integer()), schema=schema)
    op.add_column(
        "ih_identities", sa.Column("yield_reserved_until", sa.DateTime(timezone=True)), schema=schema
    )
    op.drop_constraint(
        op.f("ck_ih_lease_history_end_reason_valid"),
        "ih_lease_history",
        type_="check",
        schema=schema,
    )
    op.create_check_constraint(
        op.f("ck_ih_lease_history_end_reason_valid"),
        "ih_lease_history",
        "end_reason IS NULL OR end_reason IN "
        "('released', 'failed', 'lost', 'expired', 'revoked', 'max_hold', 'yielded')",
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_constraint(
        op.f("ck_ih_lease_history_end_reason_valid"),
        "ih_lease_history",
        type_="check",
        schema=schema,
    )
    op.create_check_constraint(
        op.f("ck_ih_lease_history_end_reason_valid"),
        "ih_lease_history",
        "end_reason IS NULL OR end_reason IN "
        "('released', 'failed', 'lost', 'expired', 'revoked', 'max_hold')",
        schema=schema,
    )
    op.drop_column("ih_identities", "yield_reserved_until", schema=schema)
    op.drop_column("ih_identities", "yield_reserved_priority", schema=schema)
