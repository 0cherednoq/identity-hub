"""request key for provider operations (AR3.3, D-335)

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    # ключ логического запроса auth.revalidate: `<request_id>:<шаг>`; для прежних строк - NULL
    op.add_column(
        "ih_provider_operations",
        sa.Column("request_key", sa.String(160), nullable=True),
        schema=_schema(),
    )
    op.create_index(
        "uq_ih_provider_operations_request",
        "ih_provider_operations",
        ["identity_id", "kind", "request_key"],
        unique=True,
        postgresql_where=sa.text("request_key IS NOT NULL AND state IN ('pending', 'running', 'succeeded')"),
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ih_provider_operations_request",
        table_name="ih_provider_operations",
        schema=_schema(),
    )
    op.drop_column("ih_provider_operations", "request_key", schema=_schema())
