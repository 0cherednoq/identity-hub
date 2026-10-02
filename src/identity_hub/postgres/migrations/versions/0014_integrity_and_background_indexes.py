"""integrity constraints, FK indexes, background query indexes, paused deliveries (F4.1a)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-02

- Индексы под фоновые запросы: строки регионов со сроком (`due_regions`) и очередь сверки
  (`stale_identities`) — R2-D15.
- Индексы под внешние ключи, по которым удаление родителя перебирало дочернюю таблицу, — R2-D16.
- Недостающие FK (`ih_notifications.channel_id`, `ih_verifications.via_identity_id`) и CHECK на
  числа прокси — R2-D17. Строки, нарушающие новые FK, исправляются: уведомления несуществующего
  канала удаляются, ссылка на удалённый аккаунт обнуляется. Отрицательный `bound_count`
  приводится к 0, `max_identities < 1` — к 1. Прокси с портом вне 1–65535 миграция не трогает:
  такая строка остановит её с ошибкой ограничения, порт исправляется вручную.
- Статус доставки `paused`: очередь отключённого endpoint'а выходит из индекса claim (R2-D15).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CHECK = "ck_ih_webhook_deliveries_status_valid"
_PROXY_CHECKS = {
    "ck_ih_proxies_bound_count_nonnegative": "bound_count >= 0",
    "ck_ih_proxies_max_identities_positive": "max_identities >= 1",
    "ck_ih_proxies_port_valid": "port BETWEEN 1 AND 65535",
}
# (имя, таблица, колонки, условие частичного индекса)
_INDEXES: tuple[tuple[str, str, list[str | sa.TextClause], str | None], ...] = (
    ("ix_ih_region_states_due", "ih_region_states", ["space_id", "region", "until"], "until IS NOT NULL"),
    (
        "ix_ih_identities_probe_queue",
        "ih_identities",
        ["space_id", "service", sa.text("probe_attempted_at NULLS FIRST"), "id"],
        "lifecycle = 'ready'",
    ),
    ("ix_ih_challenges_identity", "ih_challenges", ["identity_id", "created_at"], None),
    (
        "ix_ih_verifications_via_identity",
        "ih_verifications",
        ["via_identity_id"],
        "via_identity_id IS NOT NULL",
    ),
    ("ix_ih_webhook_endpoints_space_id", "ih_webhook_endpoints", ["space_id"], None),
    ("ix_ih_notification_channels_space_id", "ih_notification_channels", ["space_id"], None),
    ("ix_ih_notification_rules_channel_id", "ih_notification_rules", ["channel_id"], None),
    ("ix_ih_notifications_rule_id", "ih_notifications", ["rule_id"], None),
    ("ix_ih_notifications_channel_id", "ih_notifications", ["channel_id"], None),
)


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def _table(name: str) -> str:
    schema = _schema()
    return f'"{schema}".{name}' if schema else name


def _delivery_status_check(statuses: str) -> None:
    schema = _schema()
    op.drop_constraint(op.f(_STATUS_CHECK), "ih_webhook_deliveries", schema=schema, type_="check")
    op.create_check_constraint(
        op.f(_STATUS_CHECK), "ih_webhook_deliveries", f"status IN ({statuses})", schema=schema
    )


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
    # индекс правил пространства был частичным (только включённые) и не обслуживал FK
    op.drop_index("ix_ih_notification_rules_space", table_name="ih_notification_rules", schema=schema)
    op.create_index("ix_ih_notification_rules_space", "ih_notification_rules", ["space_id"], schema=schema)

    op.execute(
        f"DELETE FROM {_table('ih_notifications')} n WHERE NOT EXISTS "
        f"(SELECT 1 FROM {_table('ih_notification_channels')} c WHERE c.id = n.channel_id)"
    )
    op.create_foreign_key(
        op.f("fk_ih_notifications_channel_id_ih_notification_channels"),
        "ih_notifications",
        "ih_notification_channels",
        ["channel_id"],
        ["id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="CASCADE",
    )
    op.execute(
        f"UPDATE {_table('ih_verifications')} v SET via_identity_id = NULL "
        "WHERE via_identity_id IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM {_table('ih_identities')} i WHERE i.id = v.via_identity_id)"
    )
    op.create_foreign_key(
        op.f("fk_ih_verifications_via_identity_id_ih_identities"),
        "ih_verifications",
        "ih_identities",
        ["via_identity_id"],
        ["id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="SET NULL",
    )

    op.execute(f"UPDATE {_table('ih_proxies')} SET bound_count = 0 WHERE bound_count < 0")
    op.execute(f"UPDATE {_table('ih_proxies')} SET max_identities = 1 WHERE max_identities < 1")
    for name, condition in _PROXY_CHECKS.items():
        op.create_check_constraint(op.f(name), "ih_proxies", condition, schema=schema)

    _delivery_status_check("'pending', 'succeeded', 'failed', 'dead', 'paused'")
    op.execute(
        f"UPDATE {_table('ih_webhook_deliveries')} d SET status = 'paused' "
        f"FROM {_table('ih_webhook_endpoints')} e "
        "WHERE e.id = d.endpoint_id AND NOT e.enabled AND d.status IN ('pending', 'failed')"
    )


def downgrade() -> None:
    schema = _schema()
    op.execute(f"UPDATE {_table('ih_webhook_deliveries')} SET status = 'pending' WHERE status = 'paused'")
    _delivery_status_check("'pending', 'succeeded', 'failed', 'dead'")
    for name in _PROXY_CHECKS:
        op.drop_constraint(op.f(name), "ih_proxies", schema=schema, type_="check")
    op.drop_constraint(
        op.f("fk_ih_verifications_via_identity_id_ih_identities"),
        "ih_verifications",
        schema=schema,
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_ih_notifications_channel_id_ih_notification_channels"),
        "ih_notifications",
        schema=schema,
        type_="foreignkey",
    )
    op.drop_index("ix_ih_notification_rules_space", table_name="ih_notification_rules", schema=schema)
    op.create_index(
        "ix_ih_notification_rules_space",
        "ih_notification_rules",
        ["space_id"],
        schema=schema,
        postgresql_where=sa.text("enabled"),
    )
    for name, table, _columns, _where in reversed(_INDEXES):
        op.drop_index(name, table_name=table, schema=schema)
