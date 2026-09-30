"""idempotent administrative time rollups (D-136)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "ih_stats_rollup",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("metric", sa.String(length=128), nullable=False),
        sa.Column(
            "dims",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_size", sa.String(length=2), nullable=False),
        sa.Column("value", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "bucket_size IN ('1m', '1h', '1d')",
            name=op.f("ck_ih_stats_rollup_bucket_size_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["ih_spaces.id"],
            name=op.f("fk_ih_stats_rollup_space_id_ih_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_stats_rollup")),
        sa.UniqueConstraint(
            "space_id",
            "service",
            "metric",
            "dims",
            "bucket_start",
            "bucket_size",
            name="uq_ih_stats_rollup_key",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_ih_stats_rollup_series",
        "ih_stats_rollup",
        ["space_id", "metric", "bucket_size", "bucket_start"],
        schema=schema,
    )
    op.create_index(
        "ix_ih_stats_rollup_minute_retention",
        "ih_stats_rollup",
        ["bucket_start"],
        schema=schema,
        postgresql_where=sa.text("bucket_size = '1m'"),
    )
    op.create_table(
        "ih_stats_metric_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_stats_metric_batches")),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("ih_stats_metric_batches", schema=_schema())
    op.drop_table("ih_stats_rollup", schema=_schema())
