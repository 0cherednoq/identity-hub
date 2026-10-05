"""revision and per-space claim index of provider operations (I4.1, IMP-11)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-06

Очередь операций провайдера (вход, обновление, пробы) начинает работать (D-220): каждое изменение
записи поднимает `revision`, claim идёт по пространству — индекс `(space_id, available_at)` для
`pending`/`retryable` вместо глобального по `available_at`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLAIMABLE = "state IN ('pending', 'retryable')"


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_provider_operations",
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        schema=schema,
    )
    op.drop_index("ix_ih_provider_operations_claim", table_name="ih_provider_operations", schema=schema)
    op.create_index(
        "ix_ih_provider_operations_claim",
        "ih_provider_operations",
        ["space_id", "available_at"],
        schema=schema,
        postgresql_where=sa.text(CLAIMABLE),
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_ih_provider_operations_claim", table_name="ih_provider_operations", schema=schema)
    op.create_index(
        "ix_ih_provider_operations_claim",
        "ih_provider_operations",
        ["available_at"],
        schema=schema,
        postgresql_where=sa.text(CLAIMABLE),
    )
    op.drop_column("ih_provider_operations", "revision", schema=schema)
