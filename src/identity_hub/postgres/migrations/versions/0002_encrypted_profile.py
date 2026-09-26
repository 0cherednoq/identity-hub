"""encrypted profile columns (D-109)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    # op.add_column/drop_column не проходят через schema_translate_map: схему передаём явно
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_identities", sa.Column("profile_key_id", sa.String(length=64), nullable=True), schema=schema
    )
    op.add_column(
        "ih_identities", sa.Column("profile_wrapped_dek", sa.LargeBinary(), nullable=True), schema=schema
    )
    op.add_column("ih_identities", sa.Column("profile_nonce", sa.LargeBinary(), nullable=True), schema=schema)
    op.add_column(
        "ih_identities", sa.Column("profile_ciphertext", sa.LargeBinary(), nullable=True), schema=schema
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_column("ih_identities", "profile_ciphertext", schema=schema)
    op.drop_column("ih_identities", "profile_nonce", schema=schema)
    op.drop_column("ih_identities", "profile_wrapped_dek", schema=schema)
    op.drop_column("ih_identities", "profile_key_id", schema=schema)
