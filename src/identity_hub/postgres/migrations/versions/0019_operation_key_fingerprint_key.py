"""fingerprint key id of idempotency keys with secrets (I1.4b, IMP-04)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-05

Отпечаток команды с секретами считается по HMAC-тегам ключом отпечатков (D-208). Ключ записи
нужен, чтобы после ротации сверить повтор прежним ключом. `NULL` — команда без секретов.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.add_column(
        "ih_operation_keys", sa.Column("fingerprint_key_id", sa.String(64), nullable=True), schema=_schema()
    )


def downgrade() -> None:
    op.drop_column("ih_operation_keys", "fingerprint_key_id", schema=_schema())
