"""lease hold deadline (N1.2)

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-06

Потолок удержания аренды: `hold_deadline` в `ih_leases` и `ih_lease_history`. Продление не ставит
`expires_at` позже него, а аренда, истёкшая на своём дедлайне, закрывается причиной `max_hold`.
Колонка не индексируется: heartbeat остаётся HOT-обновлением (0021).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("ih_leases", "ih_lease_history")
OLD = "('released', 'failed', 'lost', 'expired', 'revoked')"
NEW = "('released', 'failed', 'lost', 'expired', 'revoked', 'max_hold')"
CHECK = "ck_ih_lease_history_end_reason_valid"


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _history(schema: str | None) -> str:
    return f'"{schema}".ih_lease_history' if schema else "ih_lease_history"


def _replace_check(schema: str | None, allowed: str) -> None:
    op.drop_constraint(op.f(CHECK), "ih_lease_history", type_="check", schema=schema)
    op.create_check_constraint(
        op.f(CHECK), "ih_lease_history", f"end_reason IS NULL OR end_reason IN {allowed}", schema=schema
    )


def upgrade() -> None:
    schema = _schema()
    for table in TABLES:
        op.add_column(
            table, sa.Column("hold_deadline", sa.DateTime(timezone=True), nullable=True), schema=schema
        )
    _replace_check(schema, NEW)


def downgrade() -> None:
    schema = _schema()
    op.execute(f"UPDATE {_history(schema)} SET end_reason = 'expired' WHERE end_reason = 'max_hold'")
    _replace_check(schema, OLD)
    for table in TABLES:
        op.drop_column(table, "hold_deadline", schema=schema)
