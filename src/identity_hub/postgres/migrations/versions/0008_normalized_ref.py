"""normalized external_ref and uniqueness by (service, normalized_ref) (IMP-08, D-149)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01

Backfill выполняется нормализацией по умолчанию (`str.strip`, версия 0) одним `UPDATE` с
`btrim` по тому же набору пробельных символов: миграция не знает
нормализаторов сервисов из кода приложения. Сервисы со своей нормализацией после upgrade
переводятся `hub.doctor.renormalize(service)`. Коллизии после нормализации останавливают
миграцию с безопасным списком id, данные не меняются (транзакционный DDL PostgreSQL).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# пробельные символы, которые убирает `str.strip`: backfill совпадает с нормализацией по умолчанию
_WHITESPACE = "".join(
    map(chr, [9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, *range(8192, 8203), 8232, 8233])
) + "".join(map(chr, [8239, 8287, 12288]))


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_identities", sa.Column("normalized_ref", sa.String(length=256), nullable=True), schema=schema
    )
    op.add_column(
        "ih_identities",
        sa.Column("normalize_version", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        schema=schema,
    )
    identities = sa.table(
        "ih_identities",
        sa.column("id", sa.Uuid()),
        sa.column("service", sa.String()),
        sa.column("external_ref", sa.String()),
        sa.column("normalized_ref", sa.String()),
        schema=schema,
    )
    bind = op.get_bind()
    normalized = sa.func.btrim(identities.c.external_ref, sa.literal(_WHITESPACE))
    collisions = bind.execute(
        sa.select(identities.c.service, sa.func.array_agg(sa.cast(identities.c.id, sa.String())))
        .group_by(identities.c.service, normalized)
        .having(sa.func.count() > 1)
        .order_by(identities.c.service, normalized)
    ).all()
    if collisions:
        listed = "; ".join(f"{service}: {', '.join(sorted(ids))}" for service, ids in collisions)
        raise RuntimeError(f"после нормализации external_ref совпали identity (id): {listed}")
    # один UPDATE на всю таблицу: построчный backfill держал ACCESS EXCLUSIVE минутами (R2-D12)
    bind.execute(sa.update(identities).values(normalized_ref=normalized))
    op.alter_column("ih_identities", "normalized_ref", nullable=False, schema=schema)
    op.drop_constraint("uq_ih_identities_service_ref", "ih_identities", type_="unique", schema=schema)
    op.create_unique_constraint(
        "uq_ih_identities_service_normalized_ref",
        "ih_identities",
        ["service", "normalized_ref"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_constraint(
        "uq_ih_identities_service_normalized_ref", "ih_identities", type_="unique", schema=schema
    )
    op.create_unique_constraint(
        "uq_ih_identities_service_ref", "ih_identities", ["service", "external_ref"], schema=schema
    )
    op.drop_column("ih_identities", "normalize_version", schema=schema)
    op.drop_column("ih_identities", "normalized_ref", schema=schema)
