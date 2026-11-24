"""Deferred, scoped allocation ownership and declarative policy snapshots.

Revision ID: 0045
Revises: 0044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.add_column("ih_proxy_allocations", sa.Column("policy", JSONB(none_as_null=True)), schema=schema)
    op.add_column("ih_proxy_allocations", sa.Column("expected_proxy_id", sa.Uuid()), schema=schema)
    op.create_unique_constraint(
        "uq_ih_lease_history_owner_scope",
        "ih_lease_history",
        ["lease_id", "space_id", "identity_id"],
        schema=schema,
    )
    op.drop_constraint(
        op.f("fk_ih_proxy_allocations_owner_lease_id_ih_lease_history"),
        "ih_proxy_allocations",
        type_="foreignkey",
        schema=schema,
    )
    op.create_foreign_key(
        op.f("fk_ih_proxy_allocations_owner_lease_id_ih_lease_history"),
        "ih_proxy_allocations",
        "ih_lease_history",
        ["owner_lease_id", "space_id", "identity_id"],
        ["lease_id", "space_id", "identity_id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_check_constraint(
        op.f("ck_ih_proxy_allocations_reserved_owner_required"),
        "ih_proxy_allocations",
        "state <> 'reserved' OR owner_lease_id IS NOT NULL",
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    allocations = sa.table("ih_proxy_allocations", sa.column("state"), schema=schema)
    if (
        op.get_bind()
        .execute(sa.select(sa.func.count()).select_from(allocations).where(allocations.c.state != "released"))
        .scalar_one()
    ):
        raise RuntimeError("proxy allocations: drain active assignments before downgrade")
    groups = sa.table("ih_proxy_groups", sa.column("capacity_accounting"), schema=schema)
    if (
        op.get_bind()
        .execute(
            sa.select(sa.func.count())
            .select_from(groups)
            .where(groups.c.capacity_accounting == "allocations")
        )
        .scalar_one()
    ):
        raise RuntimeError("proxy allocations: switch groups to bindings before downgrade")
    op.drop_constraint(
        op.f("ck_ih_proxy_allocations_reserved_owner_required"),
        "ih_proxy_allocations",
        type_="check",
        schema=schema,
    )
    op.drop_constraint(
        op.f("fk_ih_proxy_allocations_owner_lease_id_ih_lease_history"),
        "ih_proxy_allocations",
        type_="foreignkey",
        schema=schema,
    )
    op.create_foreign_key(
        op.f("fk_ih_proxy_allocations_owner_lease_id_ih_lease_history"),
        "ih_proxy_allocations",
        "ih_lease_history",
        ["owner_lease_id"],
        ["lease_id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="RESTRICT",
    )
    op.drop_constraint("uq_ih_lease_history_owner_scope", "ih_lease_history", type_="unique", schema=schema)
    op.drop_column("ih_proxy_allocations", "expected_proxy_id", schema=schema)
    op.drop_column("ih_proxy_allocations", "policy", schema=schema)
