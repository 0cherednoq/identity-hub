"""LRU lease selection by last_leased_at (I2.1, IMP-01)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-05

Выборка аренды сортировалась по `acquired_at` строки `ih_leases` через outer join: после
освобождения строка уходит, и порядок «наименее недавно арендованный» не держался, а индекс
выборки (`space_id, service, availability`) сортировку не обслуживал. Теперь время последнего
захвата хранится в строке identity и ставится при захвате; индекс выборки отдаёт кандидатов
сразу в порядке LRU. Backfill — последний захват из истории аренд.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREDICATE = "lifecycle = 'ready' AND auth_state = 'authenticated' AND standing IN ('good', 'limited')"


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_identities", sa.Column("last_leased_at", sa.DateTime(timezone=True), nullable=True), schema=schema
    )
    identities = sa.table("ih_identities", sa.column("id"), sa.column("last_leased_at"), schema=schema)
    history = sa.table("ih_lease_history", sa.column("identity_id"), sa.column("acquired_at"), schema=schema)
    last = (
        sa.select(history.c.identity_id, sa.func.max(history.c.acquired_at).label("acquired_at"))
        .group_by(history.c.identity_id)
        .subquery()
    )
    op.execute(
        identities.update()
        .where(identities.c.id == last.c.identity_id)
        .values(last_leased_at=last.c.acquired_at)
    )
    op.drop_index("ix_ih_identities_pick", table_name="ih_identities", schema=schema)
    op.create_index(
        "ix_ih_identities_pick",
        "ih_identities",
        ["space_id", "service", sa.text("last_leased_at ASC NULLS FIRST"), "id"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text(PREDICATE),
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_ih_identities_pick", table_name="ih_identities", schema=schema)
    op.create_index(
        "ix_ih_identities_pick",
        "ih_identities",
        ["space_id", "service", "availability"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text(PREDICATE),
    )
    op.drop_column("ih_identities", "last_leased_at", schema=schema)
