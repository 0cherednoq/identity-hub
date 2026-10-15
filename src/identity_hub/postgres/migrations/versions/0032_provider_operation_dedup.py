"""one live provider operation per identity and kind (I4.3, IMP-11)

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-06

Две живые (`pending`/`running`) операции одного вида у одного аккаунта — это две параллельные
пробы или два входа (D-223). Частичный уникальный индекс не даёт поставить вторую: она получает
`AlreadyExists`, регион в отчёте сверки — `busy`. Миграция закрывает лишние живые дубликаты как
`cancelled`, оставляя самую раннюю.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE = "state IN ('pending', 'running')"


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    table = f'"{schema}".ih_provider_operations' if schema else "ih_provider_operations"
    op.execute(
        f"UPDATE {table} SET state = 'cancelled', finished_at = now(), updated_at = now() "
        f"WHERE {LIVE} AND id NOT IN (SELECT DISTINCT ON (identity_id, kind) id FROM {table} "
        f"WHERE {LIVE} ORDER BY identity_id, kind, created_at, id)"
    )
    op.create_index(
        "uq_ih_provider_operations_live",
        "ih_provider_operations",
        ["identity_id", "kind"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text(LIVE),
    )


def downgrade() -> None:
    op.drop_index("uq_ih_provider_operations_live", table_name="ih_provider_operations", schema=_schema())
