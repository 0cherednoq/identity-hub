"""pick order column for lease selection (N1.3b)

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-06

Порядок выборки аренды отделяется от времени последней аренды. `pick_order_at` заполняется из
`last_leased_at`, индекс `ix_ih_identities_pick` переводится на новую колонку. Захват пишет обе
колонки, а аккаунт с исчерпанной квотой переносится в хвост порядка без изменения
`last_leased_at`: это поле показывается в поиске и по нему сортируют.

Индекс пересоздаётся обычным `CREATE INDEX` внутри транзакции миграции, как в 0020: на время
построения запись в `ih_identities` ждёт.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREDICATE = "lifecycle = 'ready' AND auth_state = 'authenticated' AND standing IN ('good', 'limited')"


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _table(schema: str | None) -> str:
    return f'"{schema}".ih_identities' if schema else "ih_identities"


def _reindex(schema: str | None, column: str) -> None:
    op.drop_index("ix_ih_identities_pick", table_name="ih_identities", schema=schema)
    op.create_index(
        "ix_ih_identities_pick",
        "ih_identities",
        ["space_id", "service", sa.text(f"{column} ASC NULLS FIRST"), "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text(PREDICATE),
    )


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_identities", sa.Column("pick_order_at", sa.DateTime(timezone=True), nullable=True), schema=schema
    )
    op.execute(f"UPDATE {_table(schema)} SET pick_order_at = last_leased_at WHERE last_leased_at IS NOT NULL")
    _reindex(schema, "pick_order_at")


def downgrade() -> None:
    schema = _schema()
    _reindex(schema, "last_leased_at")
    op.drop_column("ih_identities", "pick_order_at", schema=schema)
