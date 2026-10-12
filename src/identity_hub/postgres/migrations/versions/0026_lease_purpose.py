"""lease purpose and ref (N1.1)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-06

У аренды появляется назначение: `purpose` — короткая метка вида работы, `ref` — ссылка на задачу
держателя. Обе колонки необязательны и есть в `ih_leases` и `ih_lease_history`. Индексов нет:
heartbeat должен оставаться HOT-обновлением (0021).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("ih_leases", "ih_lease_history")


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    for table in TABLES:
        op.add_column(table, sa.Column("purpose", sa.String(64), nullable=True), schema=schema)
        op.add_column(table, sa.Column("ref", sa.String(256), nullable=True), schema=schema)


def downgrade() -> None:
    schema = _schema()
    for table in TABLES:
        op.drop_column(table, "ref", schema=schema)
        op.drop_column(table, "purpose", schema=schema)
