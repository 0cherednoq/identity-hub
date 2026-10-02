"""index of open lease history per space for the expired-lease sweeper (F2.2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-02

`release` не удаляет строку `ih_leases`, а ставит `expires_at = now()`, поэтому отбор истёкших
аренд по одному `expires_at` возвращал пачку давно закрытых строк и не доходил до брошенной
(R2-D01). Чистильщик теперь идёт от открытой истории (`released_at IS NULL`) своего пространства;
частичный индекс содержит только живые и брошенные аренды.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.create_index(
        "ix_ih_lease_history_open_space",
        "ih_lease_history",
        ["space_id", "lease_id"],
        unique=False,
        schema=_schema(),
        postgresql_where=sa.text("released_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ih_lease_history_open_space", table_name="ih_lease_history", schema=_schema())
