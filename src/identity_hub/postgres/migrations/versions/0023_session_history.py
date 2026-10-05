"""session records with history and lifecycle (I3.3a, IMP-10)

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-05

Сессия слота была одной строкой и перезаписывалась. Теперь у записи есть `id` и ревизия; замена
(`replace`) отзывает прежнюю запись и вставляет новую, история слота сохраняется (D-218). В слоте
одна не отозванная запись. Статусы: `valid → active`, `invalid`/`expired → invalidated` (срок
остаётся в `expires_at`). Шифртекст и AAD не меняются. Откат оставляет только не отозванные записи.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "('created', 'active', 'invalidated', 'revoked')"


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _table(schema: str | None) -> str:
    return f'"{schema}".ih_sessions' if schema else "ih_sessions"


def upgrade() -> None:
    schema = _schema()
    columns: list[sa.Column[Any]] = [
        sa.Column("id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in columns:
        op.add_column("ih_sessions", column, schema=schema)
    table = _table(schema)
    op.execute(f"UPDATE {table} SET id = gen_random_uuid() WHERE id IS NULL")
    op.alter_column("ih_sessions", "id", nullable=False, schema=schema)
    op.alter_column("ih_sessions", "status", server_default="active", schema=schema)
    op.drop_constraint(op.f("ck_ih_sessions_status_valid"), "ih_sessions", type_="check", schema=schema)
    # прежнее ограничение снято до замены значений: новых статусов оно не пропускает
    op.execute(f"UPDATE {table} SET status = CASE status WHEN 'valid' THEN 'active' ELSE 'invalidated' END")
    op.create_check_constraint(
        op.f("ck_ih_sessions_status_valid"), "ih_sessions", f"status IN {STATUSES}", schema=schema
    )
    op.drop_constraint(op.f("pk_ih_sessions"), "ih_sessions", type_="primary", schema=schema)
    op.create_primary_key(op.f("pk_ih_sessions"), "ih_sessions", ["id"], schema=schema)
    op.create_index("ix_ih_sessions_identity_slot", "ih_sessions", ["identity_id", "slot"], schema=schema)
    op.create_index(
        "uq_ih_sessions_current",
        "ih_sessions",
        ["identity_id", "slot"],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("status <> 'revoked'"),
    )


def downgrade() -> None:
    schema = _schema()
    table = _table(schema)
    op.execute(f"DELETE FROM {table} WHERE status = 'revoked'")
    op.drop_index("uq_ih_sessions_current", table_name="ih_sessions", schema=schema)
    op.drop_index("ix_ih_sessions_identity_slot", table_name="ih_sessions", schema=schema)
    op.drop_constraint(op.f("pk_ih_sessions"), "ih_sessions", type_="primary", schema=schema)
    op.create_primary_key(op.f("pk_ih_sessions"), "ih_sessions", ["identity_id", "slot"], schema=schema)
    op.drop_constraint(op.f("ck_ih_sessions_status_valid"), "ih_sessions", type_="check", schema=schema)
    op.execute(f"UPDATE {table} SET status = CASE status WHEN 'active' THEN 'valid' ELSE 'invalid' END")
    op.create_check_constraint(
        op.f("ck_ih_sessions_status_valid"),
        "ih_sessions",
        "status IN ('valid', 'invalid', 'expired')",
        schema=schema,
    )
    op.alter_column("ih_sessions", "status", server_default=None, schema=schema)
    for column in ("revoked_at", "created_at", "revision", "id"):
        op.drop_column("ih_sessions", column, schema=schema)
