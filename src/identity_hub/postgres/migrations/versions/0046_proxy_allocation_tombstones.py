"""Сохранение завершённых назначений после удаления ресурса.

Revision ID: 0046
Revises: 0045
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    table = "ih_proxy_allocations"
    for column, target in (("identity_id", "ih_identities"), ("proxy_id", "ih_proxies")):
        op.drop_constraint(op.f(f"fk_{table}_{column}_{target}"), table, type_="foreignkey", schema=schema)
        active = f"active_{column}"
        op.add_column(
            table,
            sa.Column(
                active,
                sa.Uuid(),
                sa.Computed(
                    f"CASE WHEN state::text <> 'released'::text THEN {column} ELSE NULL::uuid END",
                    persisted=True,
                ),
            ),
            schema=schema,
        )
        op.create_foreign_key(
            op.f(f"fk_{table}_{active}_{target}"),
            table,
            target,
            [active, "space_id"],
            ["id", "space_id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table}_{active}",
            table,
            [active, "space_id"],
            schema=schema,
            postgresql_where=sa.text(f"{active} IS NOT NULL"),
        )
    op.create_foreign_key(
        op.f(f"fk_{table}_space_id_ih_spaces"),
        table,
        "ih_spaces",
        ["space_id"],
        ["id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="CASCADE",
    )
    op.create_index("ix_ih_proxy_allocations_space", table, ["space_id"], schema=schema)


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    table = "ih_proxy_allocations"
    allocations = sa.table(
        table, sa.column("identity_id"), sa.column("proxy_id"), sa.column("space_id"), schema=schema
    )
    for column, target in (("identity_id", "ih_identities"), ("proxy_id", "ih_proxies")):
        resources = sa.table(target, sa.column("id"), sa.column("space_id"), schema=schema)
        exists = (
            sa.select(resources.c.id)
            .where(resources.c.id == allocations.c[column], resources.c.space_id == allocations.c.space_id)
            .exists()
        )
        if (
            op.get_bind()
            .execute(sa.select(sa.func.count()).select_from(allocations).where(~exists))
            .scalar_one()
        ):
            raise RuntimeError(
                "proxy allocations: retained tombstones refer to deleted resources; "
                "apply retention before downgrade"
            )
    op.drop_index("ix_ih_proxy_allocations_space", table_name=table, schema=schema)
    op.drop_constraint(op.f(f"fk_{table}_space_id_ih_spaces"), table, type_="foreignkey", schema=schema)
    for column, target in (("identity_id", "ih_identities"), ("proxy_id", "ih_proxies")):
        active = f"active_{column}"
        op.drop_constraint(op.f(f"fk_{table}_{active}_{target}"), table, type_="foreignkey", schema=schema)
        op.drop_index(f"ix_{table}_{active}", table_name=table, schema=schema)
        op.drop_column(table, active, schema=schema)
        op.create_foreign_key(
            op.f(f"fk_{table}_{column}_{target}"),
            table,
            target,
            [column, "space_id"],
            ["id", "space_id"],
            source_schema=schema,
            referent_schema=schema,
            ondelete="CASCADE" if column == "identity_id" else "RESTRICT",
        )
