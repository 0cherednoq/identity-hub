"""dual encrypted/plaintext storage for credentials and sessions (S5.1, D-313)

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _format_check() -> str:
    return (
        "(payload IS NOT NULL AND key_id IS NULL AND wrapped_dek IS NULL AND nonce IS NULL "
        "AND ciphertext IS NULL) OR (payload IS NULL AND key_id IS NOT NULL AND "
        "wrapped_dek IS NOT NULL AND nonce IS NOT NULL AND ciphertext IS NOT NULL)"
    )


def upgrade() -> None:
    schema = _schema()
    for table in ("ih_credentials", "ih_sessions"):
        op.add_column(table, sa.Column("payload", postgresql.JSONB(), nullable=True), schema=schema)
        for column, kind in (
            ("key_id", sa.String(length=64)),
            ("wrapped_dek", sa.LargeBinary()),
            ("nonce", sa.LargeBinary()),
            ("ciphertext", sa.LargeBinary()),
        ):
            op.alter_column(table, column, existing_type=kind, nullable=True, schema=schema)
        op.create_check_constraint("payload_format_valid", table, _format_check(), schema=schema)


def downgrade() -> None:
    schema = _schema()
    for table in ("ih_sessions", "ih_credentials"):
        op.drop_constraint("payload_format_valid", table, type_="check", schema=schema)
        op.drop_column(table, "payload", schema=schema)
        for column, kind in (
            ("key_id", sa.String(length=64)),
            ("wrapped_dek", sa.LargeBinary()),
            ("nonce", sa.LargeBinary()),
            ("ciphertext", sa.LargeBinary()),
        ):
            op.alter_column(table, column, existing_type=kind, nullable=False, schema=schema)
