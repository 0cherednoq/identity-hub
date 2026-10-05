"""several credentials per identity (I3.1a, IMP-09)

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-05

Одна запись credentials на аккаунт превращается в набор: вид (`kind`), имя (`name`), открытая
часть, жизненный цикл, срок и ревизия (D-214). Прежняя строка становится записью
`default`/`default`; её шифртекст и AAD не меняются. Откат возможен, только пока у аккаунтов
нет других записей: иначе они потерялись бы.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIFECYCLES = "('active', 'disabled', 'revoked')"


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    columns: list[sa.Column[Any]] = [
        sa.Column("id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(64), server_default="default", nullable=False),
        sa.Column("name", sa.String(128), server_default="default", nullable=False),
        sa.Column("public_value", postgresql.JSONB(), nullable=True),
        sa.Column("lifecycle", sa.String(16), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]
    for column in columns:
        op.add_column("ih_credentials", column, schema=schema)
    credentials = sa.table("ih_credentials", sa.column("id"), schema=schema)
    op.execute(credentials.update().where(credentials.c.id.is_(None)).values(id=sa.func.gen_random_uuid()))
    op.alter_column("ih_credentials", "id", nullable=False, schema=schema)
    op.drop_constraint(op.f("pk_ih_credentials"), "ih_credentials", type_="primary", schema=schema)
    op.create_primary_key(op.f("pk_ih_credentials"), "ih_credentials", ["id"], schema=schema)
    op.create_unique_constraint(
        op.f("uq_ih_credentials_identity_id"),
        "ih_credentials",
        ["identity_id", "kind", "name"],
        schema=schema,
    )
    op.create_check_constraint(
        op.f("ck_ih_credentials_lifecycle_valid"),
        "ih_credentials",
        f"lifecycle IN {LIFECYCLES}",
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    bind = op.get_bind()
    table = f'"{schema}".ih_credentials' if schema else "ih_credentials"
    extra = bind.execute(
        sa.text(f"SELECT count(*) FROM {table} WHERE kind <> 'default' OR name <> 'default'")
    )
    if extra.scalar_one():
        raise RuntimeError("у аккаунтов есть credentials кроме default: откат 0022 потерял бы их")
    op.drop_constraint(
        op.f("ck_ih_credentials_lifecycle_valid"), "ih_credentials", type_="check", schema=schema
    )
    op.drop_constraint(op.f("uq_ih_credentials_identity_id"), "ih_credentials", type_="unique", schema=schema)
    op.drop_constraint(op.f("pk_ih_credentials"), "ih_credentials", type_="primary", schema=schema)
    op.create_primary_key(op.f("pk_ih_credentials"), "ih_credentials", ["identity_id"], schema=schema)
    for column in ("created_at", "revision", "expires_at", "lifecycle", "public_value", "name", "kind", "id"):
        op.drop_column("ih_credentials", column, schema=schema)
