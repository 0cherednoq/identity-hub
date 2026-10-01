"""normalized external_ref and uniqueness by (service, normalized_ref) (IMP-08, D-149)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01

Backfill выполняется нормализацией по умолчанию (`str.strip`, версия 0): миграция не знает
нормализаторов сервисов из кода приложения. Сервисы со своей нормализацией после upgrade
переводятся `hub.doctor.renormalize(service)`. Коллизии после нормализации останавливают
миграцию с безопасным списком id, данные не меняются (транзакционный DDL PostgreSQL).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 1000


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
    rows = bind.execute(sa.select(identities.c.id, identities.c.service, identities.c.external_ref)).all()
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    updates = []
    for row in rows:
        normalized = row.external_ref.strip()
        groups[(row.service, normalized)].append(str(row.id))
        updates.append({"row_id": row.id, "normalized": normalized})
    collisions = {key: ids for key, ids in groups.items() if len(ids) > 1}
    if collisions:
        listed = "; ".join(
            f"{service}: {', '.join(sorted(ids))}" for (service, _ref), ids in sorted(collisions.items())
        )
        raise RuntimeError(f"после нормализации external_ref совпали identity (id): {listed}")
    statement = (
        sa.update(identities)
        .where(identities.c.id == sa.bindparam("row_id"))
        .values(normalized_ref=sa.bindparam("normalized"))
    )
    for start in range(0, len(updates), BATCH):
        bind.execute(statement, updates[start : start + BATCH])
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
