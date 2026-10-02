"""last probe attempt per identity for the background sync queue (F3.5)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-02

Очередь `sync.refresh_many` шла по времени последней успешной сверки. Аккаунт, чья проба не
может пройти, оставался первым навсегда и съедал лимит каждого запуска (R2-F02). Отметка
попытки ставится при любом исходе, очередь сортируется по ней.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    op.add_column(
        "ih_identities",
        sa.Column("probe_attempted_at", sa.DateTime(timezone=True), nullable=True),
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_column("ih_identities", "probe_attempted_at", schema=_schema())
