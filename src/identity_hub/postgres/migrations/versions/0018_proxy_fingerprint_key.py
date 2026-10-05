"""key id of the keyed proxy fingerprint (I1.4a, IMP-04, R2-S16)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-05

Отпечаток прокси был SHA-256 без ключа от `scheme|host|port|username`: логин подбирался
перебором по известному хосту (R2-S16). Теперь это HMAC-SHA-256 ключом из кольца отпечатков
(D-207), в строке — его `fingerprint_key_id`. У миграции ключей нет: прежние значения остаются
с `NULL`, находятся при поиске и пересчитываются `identity-hub doctor rekey-fingerprints`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.add_column(
        "ih_proxies", sa.Column("fingerprint_key_id", sa.String(64), nullable=True), schema=_schema()
    )


def downgrade() -> None:
    op.drop_column("ih_proxies", "fingerprint_key_id", schema=_schema())
