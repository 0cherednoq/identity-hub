"""secret value fingerprints and validity markers (S9.1, D-319)

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    for table in ("ih_credentials", "ih_sessions"):
        op.add_column(table, sa.Column("value_fingerprint", sa.String(64)), schema=schema)
        op.add_column(table, sa.Column("last_verified_at", sa.DateTime(timezone=True)), schema=schema)
        op.add_column(table, sa.Column("last_rejected_at", sa.DateTime(timezone=True)), schema=schema)
        op.add_column(table, sa.Column("reject_code", sa.String(64)), schema=schema)


def downgrade() -> None:
    schema = _schema()
    for table in ("ih_sessions", "ih_credentials"):
        op.drop_column(table, "reject_code", schema=schema)
        op.drop_column(table, "last_rejected_at", schema=schema)
        op.drop_column(table, "last_verified_at", schema=schema)
        op.drop_column(table, "value_fingerprint", schema=schema)
