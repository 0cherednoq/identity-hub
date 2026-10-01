"""idempotency protocol: ih_operation_keys keyed by (namespace, key) (IMP-03, F1.1, D-153)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02

Ключ идемпотентности перестаёт быть глобальным: первичный ключ — `(namespace, key)`, где
namespace включает пространство и семейство команд. Существующие ключи операций переносятся в
namespace своего пространства с `fingerprint_version = 0`: повтор с тем же ключом после
обновления по-прежнему считается повтором, сверяется только вид операции.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ih_operation_keys"
FK = "fk_ih_operation_keys_identity_id_ih_identities"
LEGACY_TTL = "7 days"  # прежний Retention.operation_keys по умолчанию


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _qualified(schema: str | None) -> str:
    return f'"{schema}".{TABLE}' if schema else TABLE


def upgrade() -> None:
    schema = _schema()
    table = _qualified(schema)
    # ключ живёт до expires_at и не исчезает вместе с аккаунтом: иначе повтор задачи выполнился бы заново
    op.drop_constraint(FK, TABLE, type_="foreignkey", schema=schema)
    op.alter_column(TABLE, "kind", new_column_name="operation", schema=schema)
    op.alter_column(TABLE, "identity_id", nullable=True, schema=schema)
    op.add_column(TABLE, sa.Column("namespace", sa.String(length=128), nullable=True), schema=schema)
    op.add_column(
        TABLE,
        sa.Column("fingerprint_version", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        schema=schema,
    )
    op.add_column(TABLE, sa.Column("fingerprint", sa.String(length=64), nullable=True), schema=schema)
    op.add_column(
        TABLE,
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema=schema,
    )
    op.add_column(TABLE, sa.Column("result_type", sa.String(length=64), nullable=True), schema=schema)
    op.add_column(TABLE, sa.Column("result_version", sa.SmallInteger(), nullable=True), schema=schema)
    op.add_column(
        TABLE,
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema=schema,
    )
    op.add_column(TABLE, sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True), schema=schema)
    op.execute(
        rf"UPDATE {table} SET namespace = space_id\:\:text || '\:operation', "
        f"expires_at = created_at + interval '{LEGACY_TTL}'"
    )
    op.alter_column(TABLE, "namespace", nullable=False, schema=schema)
    op.alter_column(TABLE, "expires_at", nullable=False, schema=schema)
    op.alter_column(TABLE, "fingerprint_version", server_default=None, schema=schema)
    op.alter_column(TABLE, "attempt", server_default=None, schema=schema)
    op.drop_constraint("pk_ih_operation_keys", TABLE, type_="primary", schema=schema)
    op.create_primary_key("pk_ih_operation_keys", TABLE, ["namespace", "key"], schema=schema)
    op.drop_index("ix_ih_operation_keys_created_at", table_name=TABLE, schema=schema)
    op.create_index("ix_ih_operation_keys_expires_at", TABLE, ["expires_at"], unique=False, schema=schema)


def downgrade() -> None:
    schema = _schema()
    table = _qualified(schema)
    # Прежняя схема хранит только ключи операций с владельцем и глобально уникальным key:
    # остальные записи в неё не помещаются. Ключи эфемерны (срок хранения — дни).
    identities = f'"{schema}".ih_identities' if schema else "ih_identities"
    op.execute(
        rf"DELETE FROM {table} k WHERE k.namespace NOT LIKE '%\:operation' OR NOT EXISTS "
        f"(SELECT 1 FROM {identities} i WHERE i.id = k.identity_id AND i.space_id = k.space_id)"
    )
    op.execute(
        f"DELETE FROM {table} a USING {table} b "
        "WHERE a.key = b.key AND (a.created_at, a.namespace) < (b.created_at, b.namespace)"
    )
    op.drop_index("ix_ih_operation_keys_expires_at", table_name=TABLE, schema=schema)
    op.create_index("ix_ih_operation_keys_created_at", TABLE, ["created_at"], unique=False, schema=schema)
    op.drop_constraint("pk_ih_operation_keys", TABLE, type_="primary", schema=schema)
    op.create_primary_key("pk_ih_operation_keys", TABLE, ["key"], schema=schema)
    op.drop_column(TABLE, "expires_at", schema=schema)
    op.drop_column(TABLE, "result", schema=schema)
    op.drop_column(TABLE, "result_version", schema=schema)
    op.drop_column(TABLE, "result_type", schema=schema)
    op.drop_column(TABLE, "attempt", schema=schema)
    op.drop_column(TABLE, "fingerprint", schema=schema)
    op.drop_column(TABLE, "fingerprint_version", schema=schema)
    op.drop_column(TABLE, "namespace", schema=schema)
    op.alter_column(TABLE, "identity_id", nullable=False, schema=schema)
    op.alter_column(TABLE, "operation", new_column_name="kind", schema=schema)
    op.create_foreign_key(
        FK,
        TABLE,
        "ih_identities",
        ["identity_id", "space_id"],
        ["id", "space_id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="CASCADE",
    )
