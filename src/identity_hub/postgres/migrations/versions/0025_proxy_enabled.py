"""Proxy enabled flag set by the operator (C1.4a, D-224)

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-12

Выключение прокси оператором — отдельное от здоровья измерение: `status` остаётся наблюдаемым
фактом, `enabled` — намерение. Выключенный прокси не выдаётся, автоматика его не трогает,
причина, автор и время хранятся в строке. Существующие прокси включены.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_proxies",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        schema=schema,
    )
    op.add_column("ih_proxies", sa.Column("disabled_reason", sa.Text(), nullable=True), schema=schema)
    op.add_column("ih_proxies", sa.Column("disabled_by", sa.String(length=128), nullable=True), schema=schema)
    op.add_column(
        "ih_proxies", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True), schema=schema
    )


def downgrade() -> None:
    schema = _schema()
    for column in ("disabled_at", "disabled_by", "disabled_reason", "enabled"):
        op.drop_column("ih_proxies", column, schema=schema)
