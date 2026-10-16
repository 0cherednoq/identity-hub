"""plaintext storage for all secret stores, not only credentials/sessions (S13.1, D-323)

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-16
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _group_cols(prefix: str) -> tuple[tuple[str, Any], ...]:
    return (
        (f"{prefix}key_id", sa.String(length=64)),
        (f"{prefix}wrapped_dek", sa.LargeBinary()),
        (f"{prefix}nonce", sa.LargeBinary()),
        (f"{prefix}ciphertext", sa.LargeBinary()),
    )


def _format_check(prefix: str = "", *, optional: bool = False) -> str:
    p = prefix
    enc = (
        f"{p}key_id IS NOT NULL AND {p}wrapped_dek IS NOT NULL "
        f"AND {p}nonce IS NOT NULL AND {p}ciphertext IS NOT NULL"
    )
    enc_null = f"{p}key_id IS NULL AND {p}wrapped_dek IS NULL AND {p}nonce IS NULL AND {p}ciphertext IS NULL"
    clauses = [f"({p}payload IS NOT NULL AND {enc_null})", f"({p}payload IS NULL AND {enc})"]
    if optional:
        clauses.append(f"({p}payload IS NULL AND {enc_null})")
    return " OR ".join(clauses)


# таблицы с «безымянной» группой секрета, где секрет обязателен (ровно один формат)
_MANDATORY = ("ih_proxies", "ih_import_items", "ih_webhook_endpoints", "ih_notification_channels")


def upgrade() -> None:
    schema = _schema()

    # 1. Таблицы с обязательным секретом в безымянной группе: добавить payload, снять NOT NULL.
    for table in _MANDATORY:
        op.add_column(table, sa.Column("payload", postgresql.JSONB(), nullable=True), schema=schema)
        for column, kind in _group_cols(""):
            op.alter_column(table, column, existing_type=kind, nullable=True, schema=schema)
        op.create_check_constraint("payload_format_valid", table, _format_check(), schema=schema)

    # 2. Профиль (необязательный секрет; колонки уже nullable).
    op.add_column(
        "ih_identities", sa.Column("profile_payload", postgresql.JSONB(), nullable=True), schema=schema
    )
    op.create_check_constraint(
        "profile_payload_format_valid",
        "ih_identities",
        _format_check("profile_", optional=True),
        schema=schema,
    )

    # 3. Evidence верификаций (необязательный секрет; колонки уже nullable).
    op.add_column(
        "ih_verifications", sa.Column("evidence_payload", postgresql.JSONB(), nullable=True), schema=schema
    )
    op.create_check_constraint(
        "evidence_payload_format_valid",
        "ih_verifications",
        _format_check("evidence_", optional=True),
        schema=schema,
    )

    # 4. Challenge: context (обязателен) + answer (открытая строка в plaintext-режиме).
    op.add_column(
        "ih_challenges", sa.Column("context_payload", postgresql.JSONB(), nullable=True), schema=schema
    )
    op.add_column("ih_challenges", sa.Column("answer_payload", sa.Text(), nullable=True), schema=schema)
    for column, kind in _group_cols("context_"):
        op.alter_column("ih_challenges", column, existing_type=kind, nullable=True, schema=schema)
    op.create_check_constraint(
        "context_payload_format_valid", "ih_challenges", _format_check("context_"), schema=schema
    )
    op.create_check_constraint(
        "answer_payload_format_valid",
        "ih_challenges",
        "NOT (answer_payload IS NOT NULL AND answer_ciphertext IS NOT NULL)",
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()

    # В 0036 envelope-колонки снова обязательны. Требуем явной конвертации, чтобы downgrade
    # не закончился неочевидной ошибкой ALTER COLUMN и не потерял открытые секреты.
    bind = op.get_bind()
    plaintext_columns = (
        *((table, "payload") for table in _MANDATORY),
        ("ih_identities", "profile_payload"),
        ("ih_verifications", "evidence_payload"),
        ("ih_challenges", "context_payload"),
    )
    for table, column in plaintext_columns:
        source = sa.table(table, sa.column(column), schema=schema)
        if bind.execute(sa.select(sa.literal(1)).where(source.c[column].is_not(None)).limit(1)).first():
            raise RuntimeError(
                "0037 downgrade requires encrypted secrets; run "
                "`identity-hub secrets convert --to encrypted` first"
            )

    op.drop_constraint("answer_payload_format_valid", "ih_challenges", type_="check", schema=schema)
    op.drop_constraint("context_payload_format_valid", "ih_challenges", type_="check", schema=schema)
    for column, kind in _group_cols("context_"):
        op.alter_column("ih_challenges", column, existing_type=kind, nullable=False, schema=schema)
    op.drop_column("ih_challenges", "answer_payload", schema=schema)
    op.drop_column("ih_challenges", "context_payload", schema=schema)

    op.drop_constraint("evidence_payload_format_valid", "ih_verifications", type_="check", schema=schema)
    op.drop_column("ih_verifications", "evidence_payload", schema=schema)

    op.drop_constraint("profile_payload_format_valid", "ih_identities", type_="check", schema=schema)
    op.drop_column("ih_identities", "profile_payload", schema=schema)

    for table in reversed(_MANDATORY):
        op.drop_constraint("payload_format_valid", table, type_="check", schema=schema)
        for column, kind in _group_cols(""):
            op.alter_column(table, column, existing_type=kind, nullable=False, schema=schema)
        op.drop_column(table, "payload", schema=schema)
