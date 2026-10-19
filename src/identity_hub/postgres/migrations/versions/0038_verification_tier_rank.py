"""denormalized verification tier rank for public read schema (SP3.1, D-325)

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.add_column(
        "ih_identities",
        sa.Column("verification_tier_rank", sa.SmallInteger(), nullable=True),
        schema=_schema(),
    )
    op.create_index(
        "ix_ih_identities_space_id",
        "ih_identities",
        ["space_id", "id"],
        schema=_schema(),
    )
    op.create_index(
        "ix_ih_event_cursors_position",
        "ih_event_cursors",
        ["last_tx_id", "last_id"],
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ih_event_cursors_position",
        table_name="ih_event_cursors",
        schema=_schema(),
    )
    op.drop_index(
        "ix_ih_identities_space_id",
        table_name="ih_identities",
        schema=_schema(),
    )
    op.drop_column("ih_identities", "verification_tier_rank", schema=_schema())
