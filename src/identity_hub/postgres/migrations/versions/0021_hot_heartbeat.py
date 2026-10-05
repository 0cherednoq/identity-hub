"""HOT heartbeat: no index on heartbeat columns, fillfactor 70 (I2.2a, IMP-02)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-05

Индекс на `ih_leases.expires_at` делал каждый heartbeat не-HOT обновлением, а частичный индекс
открытой истории по `last_heartbeat_at` — то же для истории: таблицы и индексы раздувались.
Чистильщик истёкших аренд идёт от открытой истории пространства
(`ix_ih_lease_history_open_space`, миграция 0011), эти индексы ему не нужны. `fillfactor = 70`
оставляет на странице место под новую версию строки.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _table(schema: str | None) -> str:
    return f'"{schema}".ih_leases' if schema else "ih_leases"


def upgrade() -> None:
    schema = _schema()
    op.drop_index("ix_ih_leases_expires_at", table_name="ih_leases", schema=schema)
    op.drop_index("ix_ih_lease_history_open", table_name="ih_lease_history", schema=schema)
    op.execute(f"ALTER TABLE {_table(schema)} SET (fillfactor = 70)")


def downgrade() -> None:
    schema = _schema()
    op.execute(f"ALTER TABLE {_table(schema)} RESET (fillfactor)")
    op.create_index(
        "ix_ih_lease_history_open",
        "ih_lease_history",
        ["last_heartbeat_at"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index("ix_ih_leases_expires_at", "ih_leases", ["expires_at"], unique=False, schema=schema)
