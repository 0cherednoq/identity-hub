"""proxy bound_count (D-117)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.add_column(
        "ih_proxies",
        sa.Column("bound_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_column("ih_proxies", "bound_count", schema=_schema())
