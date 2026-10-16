"""service platform URL (S11.1, D-321)

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.add_column("ih_services", sa.Column("url", sa.Text()), schema=_schema())


def downgrade() -> None:
    op.drop_column("ih_services", "url", schema=_schema())
