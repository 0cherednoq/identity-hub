"""lease idempotency key is scoped to its space (IMP-03, F1.2)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-02

Глобальный уникальный индекс `uq_ih_leases_idempotency` давал сырой `IntegrityError`, когда тот же
ключ приходил из другого пространства. Единственность ключа аренды теперь обеспечивает запись
`ih_operation_keys` в namespace `<space_id>:lease`; в `ih_leases` остаётся индекс для поиска живой
аренды по ключу в пределах пространства.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WHERE = sa.text("idempotency_key IS NOT NULL")


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.drop_index("uq_ih_leases_idempotency", table_name="ih_leases", schema=schema)
    op.create_index(
        "ix_ih_leases_idempotency",
        "ih_leases",
        ["space_id", "idempotency_key"],
        unique=False,
        schema=schema,
        postgresql_where=WHERE,
    )


def downgrade() -> None:
    schema = _schema()
    table = f'"{schema}".ih_leases' if schema else "ih_leases"
    # Прежний индекс уникален по всей базе: ключ остаётся у самой свежей аренды, у остальных снимается.
    op.execute(
        f"UPDATE {table} a SET idempotency_key = NULL FROM {table} b "
        "WHERE a.idempotency_key = b.idempotency_key "
        "AND (a.acquired_at, a.identity_id) < (b.acquired_at, b.identity_id)"
    )
    op.drop_index("ix_ih_leases_idempotency", table_name="ih_leases", schema=schema)
    op.create_index(
        "uq_ih_leases_idempotency",
        "ih_leases",
        ["idempotency_key"],
        unique=True,
        schema=schema,
        postgresql_where=WHERE,
    )
