"""Снимок политики каждой выдачи, в том числе использования прежней привязки.

Revision ID: 0047
Revises: 0046
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    for table in ("ih_leases", "ih_lease_history"):
        op.add_column(table, sa.Column("proxy_request", JSONB(none_as_null=True)), schema=schema)


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    leases = sa.table("ih_leases", sa.column("proxy_request"), sa.column("expires_at"), schema=schema)
    live = (
        sa.select(leases.c.expires_at)
        .where(leases.c.proxy_request.is_not(None), leases.c.expires_at > sa.func.now())
        .exists()
    )
    if op.get_bind().execute(sa.select(live)).scalar_one():
        raise RuntimeError("proxy lease request: drain live leases before downgrade")
    for table in ("ih_lease_history", "ih_leases"):
        op.drop_column(table, "proxy_request", schema=schema)
