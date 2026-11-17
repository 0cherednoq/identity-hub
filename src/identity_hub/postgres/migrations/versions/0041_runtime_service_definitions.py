"""runtime service definitions

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    return op.get_context().version_table_schema


def upgrade() -> None:
    table = "ih_services"
    schema = _schema()
    op.alter_column(
        "ih_events",
        "space_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
        schema=schema,
    )
    op.add_column(
        table,
        sa.Column("origin", sa.String(16), nullable=False, server_default="static"),
        schema=schema,
    )
    op.add_column(table, sa.Column("template", sa.String(128), nullable=True), schema=schema)
    op.add_column(table, sa.Column("config", postgresql.JSONB(), nullable=True), schema=schema)
    op.add_column(
        table,
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema=schema,
    )
    op.add_column(
        table,
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="1"),
        schema=schema,
    )
    op.add_column(
        table,
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema=schema,
    )
    op.add_column(
        table,
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema=schema,
    )
    op.add_column(table, sa.Column("idempotency_key", sa.String(256), nullable=True), schema=schema)
    op.add_column(table, sa.Column("idempotency_fingerprint", sa.String(128), nullable=True), schema=schema)
    op.create_check_constraint(
        "ck_ih_services_origin_valid", table, "origin IN ('static', 'runtime')", schema=schema
    )
    op.create_check_constraint("ck_ih_services_revision_positive", table, "revision >= 1", schema=schema)
    op.create_check_constraint(
        "ck_ih_services_runtime_definition_valid",
        table,
        "(origin = 'static' AND template IS NULL AND config IS NULL) OR "
        "(origin = 'runtime' AND template IS NOT NULL AND config IS NOT NULL)",
        schema=schema,
    )
    op.create_unique_constraint("uq_ih_services_idempotency_key", table, ["idempotency_key"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    qualified = f'"{schema}".ih_services' if schema else "ih_services"
    runtime_count = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {qualified} WHERE origin = 'runtime'")
    )
    if runtime_count.scalar_one():
        raise RuntimeError("cannot downgrade while runtime service definitions exist")
    qualified_events = f'"{schema}".ih_events' if schema else "ih_events"
    global_event_count = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {qualified_events} WHERE space_id IS NULL")
    )
    if global_event_count.scalar_one():
        raise RuntimeError("cannot downgrade while global service definition events exist")
    table = "ih_services"
    op.drop_constraint("uq_ih_services_idempotency_key", table, type_="unique", schema=schema)
    op.drop_constraint("ck_ih_services_runtime_definition_valid", table, type_="check", schema=schema)
    op.drop_constraint("ck_ih_services_revision_positive", table, type_="check", schema=schema)
    op.drop_constraint("ck_ih_services_origin_valid", table, type_="check", schema=schema)
    for column in (
        "idempotency_fingerprint", "idempotency_key", "updated_at", "created_at", "revision",
        "enabled", "config", "template", "origin",
    ):
        op.drop_column(table, column, schema=schema)
    op.alter_column(
        "ih_events",
        "space_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
        schema=schema,
    )
