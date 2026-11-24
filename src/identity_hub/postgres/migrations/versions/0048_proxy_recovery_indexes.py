"""Индексы ограниченного recovery/retention назначений прокси.

Revision ID: 0048
Revises: 0047
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.create_index("ix_ih_proxies_space_id", "ih_proxies", ["space_id", "id"], schema=schema)
    op.create_index(
        "ix_ih_proxy_allocations_pending",
        "ih_proxy_allocations",
        ["space_id", "id"],
        schema=schema,
        postgresql_where=sa.text("state IN ('reserved', 'draining')"),
    )
    op.create_index(
        "ix_ih_proxy_allocations_released",
        "ih_proxy_allocations",
        ["space_id", "ended_at", "id"],
        schema=schema,
        postgresql_where=sa.text("state = 'released'"),
    )
    op.create_index(
        "ix_ih_lease_history_proxy_allocation",
        "ih_lease_history",
        ["proxy_allocation_id", "space_id"],
        schema=schema,
        postgresql_where=sa.text("proxy_allocation_id IS NOT NULL"),
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index("ix_ih_proxies_space_id", table_name="ih_proxies", schema=schema)
    op.drop_index("ix_ih_lease_history_proxy_allocation", table_name="ih_lease_history", schema=schema)
    op.drop_index("ix_ih_proxy_allocations_released", table_name="ih_proxy_allocations", schema=schema)
    op.drop_index("ix_ih_proxy_allocations_pending", table_name="ih_proxy_allocations", schema=schema)
