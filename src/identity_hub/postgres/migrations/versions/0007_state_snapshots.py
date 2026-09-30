"""hourly current-state snapshots with coverage (D-137)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "ih_state_snapshot_coverage",
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("complete", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["ih_spaces.id"],
            name=op.f("fk_ih_state_snapshot_coverage_space_id_ih_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("space_id", "taken_at", name=op.f("pk_ih_state_snapshot_coverage")),
        schema=schema,
    )
    op.create_table(
        "ih_state_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("region", sa.String(length=192), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count >= 0", name=op.f("ck_ih_state_snapshots_count_nonnegative")),
        sa.ForeignKeyConstraint(
            ["space_id", "taken_at"],
            ["ih_state_snapshot_coverage.space_id", "ih_state_snapshot_coverage.taken_at"],
            name=op.f("fk_ih_state_snapshots_space_id_ih_state_snapshot_coverage"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_state_snapshots")),
        sa.UniqueConstraint(
            "space_id",
            "service",
            "region",
            "state",
            "taken_at",
            name="uq_ih_state_snapshots_key",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_ih_state_snapshots_series",
        "ih_state_snapshots",
        ["space_id", "region", "taken_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("ih_state_snapshots", schema=schema)
    op.drop_table("ih_state_snapshot_coverage", schema=schema)
