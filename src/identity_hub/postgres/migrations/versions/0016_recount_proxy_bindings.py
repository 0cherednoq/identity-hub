"""recount ih_proxies.bound_count from bindings (F4.2)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-03

Ревизия 0004 добавила счётчик `bound_count` со значением 0 и не заполнила его из уже
существовавших привязок (R2-D12): на базе, где привязки появились раньше счётчика, прокси
выглядели свободными и получали аккаунты сверх `max_identities`. Счётчик пересчитывается
из `ih_bindings`; строки с верным значением не трогаются.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema: str | None = op.get_context().version_table_schema
    prefix = f'"{schema}".' if schema else ""
    op.execute(
        f"UPDATE {prefix}ih_proxies p SET bound_count = c.bound "
        f"FROM (SELECT x.id, count(b.identity_id) AS bound FROM {prefix}ih_proxies x "
        f"LEFT JOIN {prefix}ih_bindings b ON b.proxy_id = x.id GROUP BY x.id) c "
        "WHERE c.id = p.id AND p.bound_count <> c.bound"
    )


def downgrade() -> None:
    """Пересчёт данных не откатывается: прежние значения были неверными."""
