"""notification dedup per (rule, event) and channel pause after 429 (F3.3)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-02

Диспетчер доставляет события at-least-once: повтор пачки создавал второе уведомление по тому
же правилу и событию (R2-D13). Для уведомлений без дайджеста вводится уникальность
`(rule_id, event_ids[1])`; дубли, накопленные до миграции, удаляются (остаётся самая ранняя
строка). `paused_until` — пауза канала после ответа 429 (R2-F03).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    table = f'"{schema}".ih_notifications' if schema else "ih_notifications"
    op.add_column(
        "ih_notification_channels",
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.execute(
        f"DELETE FROM {table} a USING {table} b "
        "WHERE a.digest_key IS NULL AND b.digest_key IS NULL "
        "AND a.rule_id = b.rule_id AND a.event_ids[1] = b.event_ids[1] AND a.id > b.id"
    )
    op.create_index(
        "uq_ih_notifications_rule_event",
        "ih_notifications",
        ["rule_id", sa.text("(event_ids[1])")],
        unique=True,
        schema=schema,
        postgresql_where=sa.text("digest_key IS NULL"),
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("uq_ih_notifications_rule_event", table_name="ih_notifications", schema=schema)
    op.drop_column("ih_notification_channels", "paused_until", schema=schema)
