"""aggregate revision of identity and in events (I1.2a, IMP-07)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-05

`revision` — число команд, изменивших агрегат identity (D-205). До неё `Identity.revision`
отдавал `event_seq`, который растёт на каждое событие, в том числе от аренды. Backfill берёт
`GREATEST(event_seq, 1)`: ревизии, которые клиенты уже видели, не уменьшаются. В событии —
ревизия identity после команды; у событий до миграции — `NULL`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_identities",
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        schema=schema,
    )
    identities = sa.table("ih_identities", sa.column("revision"), sa.column("event_seq"), schema=schema)
    op.execute(
        identities.update()
        .where(identities.c.event_seq > 1)
        .values(revision=sa.func.greatest(identities.c.event_seq, 1))
    )
    op.add_column("ih_events", sa.Column("revision", sa.BigInteger(), nullable=True), schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_column("ih_events", "revision", schema=schema)
    op.drop_column("ih_identities", "revision", schema=schema)
