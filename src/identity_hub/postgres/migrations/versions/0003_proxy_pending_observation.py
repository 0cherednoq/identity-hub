"""proxy pending observation (D-116)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.add_column(
        "ih_proxies", sa.Column("pending_observation", sa.String(length=16), nullable=True), schema=_schema()
    )


def downgrade() -> None:
    op.drop_column("ih_proxies", "pending_observation", schema=_schema())
