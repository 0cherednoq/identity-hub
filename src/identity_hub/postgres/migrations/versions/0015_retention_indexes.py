"""indexes for retention cleanup (F4.1b)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-02

Чистка по срокам хранения читала таблицы целиком (R2-D10). Индексы под её условия:
окна квот — по `(window_seconds, window_start)` (прежний индекс по одному `window_start` условию
«окно завершилось» не помогал и удалён), доставки — по `created_at` и по `event_id` (доставки
удаляемых событий), уведомления — по выражению `COALESCE(sent_at, scheduled_at)`, завершённые
импорты и операции провайдера — по `finished_at`, квитанции метрик — по `created_at`, закрытые
вопросы — по `resolved_at`, часовые и суточные корзины — по `(bucket_size, bucket_start)`.
События и история аренд чистятся по пространствам и используют существующие индексы.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (имя, таблица, колонки, условие частичного индекса)
_INDEXES: tuple[tuple[str, str, list[str | sa.TextClause], str | None], ...] = (
    ("ix_ih_quota_usage_window", "ih_quota_usage", ["window_seconds", "window_start"], None),
    ("ix_ih_webhook_deliveries_created_at", "ih_webhook_deliveries", ["created_at"], None),
    ("ix_ih_webhook_deliveries_event_id", "ih_webhook_deliveries", ["event_id"], None),
    (
        "ix_ih_notifications_retention",
        "ih_notifications",
        [sa.text("(COALESCE(sent_at, scheduled_at))")],
        None,
    ),
    ("ix_ih_import_runs_finished", "ih_import_runs", ["finished_at"], "finished_at IS NOT NULL"),
    (
        "ix_ih_provider_operations_finished",
        "ih_provider_operations",
        ["finished_at"],
        "finished_at IS NOT NULL",
    ),
    ("ix_ih_stats_metric_batches_created_at", "ih_stats_metric_batches", ["created_at"], None),
    ("ix_ih_challenges_resolved", "ih_challenges", ["resolved_at"], "resolved_at IS NOT NULL"),
    (
        "ix_ih_stats_rollup_coarse_retention",
        "ih_stats_rollup",
        ["bucket_size", "bucket_start"],
        "bucket_size IN ('1h', '1d')",
    ),
)


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    for name, table, columns, where in _INDEXES:
        op.create_index(
            name,
            table,
            columns,
            unique=False,
            schema=schema,
            postgresql_where=sa.text(where) if where else None,
        )
    op.drop_index("ix_ih_quota_usage_window_start", table_name="ih_quota_usage", schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.create_index("ix_ih_quota_usage_window_start", "ih_quota_usage", ["window_start"], schema=schema)
    for name, table, _columns, _where in reversed(_INDEXES):
        op.drop_index(name, table_name=table, schema=schema)
