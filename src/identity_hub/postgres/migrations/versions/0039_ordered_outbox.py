"""optional ordered outbox for webhook endpoints (I5.3, D-328)

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    # прежняя неупорядоченная семантика сохраняется: ordered по умолчанию выключен (D-328)
    op.add_column(
        "ih_webhook_endpoints",
        sa.Column("ordered", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=_schema(),
    )
    # ключ ресурса и его ревизия: заполняются при постановке доставки, для прежних строк — NULL
    op.add_column(
        "ih_webhook_deliveries",
        sa.Column("resource_key", sa.String(64), nullable=True),
        schema=_schema(),
    )
    op.add_column(
        "ih_webhook_deliveries",
        sa.Column("resource_revision", sa.BigInteger(), nullable=True),
        schema=_schema(),
    )
    op.create_index(
        "ix_ih_webhook_deliveries_order",
        "ih_webhook_deliveries",
        ["endpoint_id", "resource_key", "resource_revision", "id"],
        postgresql_where=sa.text("status <> 'succeeded' AND resource_key IS NOT NULL"),
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ih_webhook_deliveries_order",
        table_name="ih_webhook_deliveries",
        schema=_schema(),
    )
    op.drop_column("ih_webhook_deliveries", "resource_revision", schema=_schema())
    op.drop_column("ih_webhook_deliveries", "resource_key", schema=_schema())
    op.drop_column("ih_webhook_endpoints", "ordered", schema=_schema())
