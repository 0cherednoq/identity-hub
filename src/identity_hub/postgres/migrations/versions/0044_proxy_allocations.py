"""Proxy allocation ledger and persisted group accounting.

Revision ID: 0044
Revises: 0043
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    groups = op.create_table(
        "ih_proxy_groups",
        sa.Column("space_id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("capacity_accounting", sa.String(16), nullable=False, server_default="bindings"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["space_id"], ["ih_spaces.id"], ondelete="CASCADE"),
        sa.CheckConstraint("capacity_accounting IN ('bindings', 'allocations')", name="accounting_valid"),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
        schema=schema,
    )
    proxies = sa.table("ih_proxies", sa.column("space_id"), sa.column("pool"), schema=schema)
    op.execute(
        sa.insert(groups).from_select(
            ["space_id", "name"], sa.select(proxies.c.space_id, proxies.c.pool).distinct()
        )
    )
    op.create_table(
        "ih_proxy_allocations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("proxy_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("owner_lease_id", sa.Uuid()),
        sa.Column("expected_binding_id", sa.Uuid()),
        sa.Column("policy_fingerprint", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(64)),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"], ["ih_identities.id", "ih_identities.space_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["proxy_id", "space_id"], ["ih_proxies.id", "ih_proxies.space_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["owner_lease_id"], ["ih_lease_history.lease_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "id", "space_id", "identity_id", "proxy_id", name="uq_ih_proxy_allocations_scope"
        ),
        sa.CheckConstraint("state IN ('reserved', 'bound', 'draining', 'released')", name="state_valid"),
        sa.CheckConstraint("(state = 'released') = (ended_at IS NOT NULL)", name="ended_at_valid"),
        schema=schema,
    )
    op.create_index(
        "uq_ih_proxy_allocations_active_pair",
        "ih_proxy_allocations",
        ["space_id", "identity_id", "proxy_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'released'"),
        schema=schema,
    )
    op.create_index(
        "ix_ih_proxy_allocations_proxy",
        "ih_proxy_allocations",
        ["proxy_id", "space_id", "state"],
        schema=schema,
    )
    op.create_index(
        "ix_ih_proxy_allocations_owner", "ih_proxy_allocations", ["owner_lease_id", "space_id"], schema=schema
    )
    op.create_index(
        "ix_ih_proxy_allocations_identity", "ih_proxy_allocations", ["identity_id", "space_id"], schema=schema
    )
    for name in ("reserved_count", "draining_count"):
        op.add_column(
            "ih_proxies", sa.Column(name, sa.Integer(), nullable=False, server_default="0"), schema=schema
        )
        op.create_check_constraint(
            op.f(f"ck_ih_proxies_{name}_nonnegative"), "ih_proxies", f"{name} >= 0", schema=schema
        )
    op.add_column("ih_bindings", sa.Column("allocation_id", sa.Uuid()), schema=schema)
    op.create_index("ix_ih_bindings_allocation_id", "ih_bindings", ["allocation_id"], schema=schema)
    op.create_foreign_key(
        op.f("fk_ih_bindings_allocation_id_ih_proxy_allocations"),
        "ih_bindings",
        "ih_proxy_allocations",
        ["allocation_id", "space_id", "identity_id", "proxy_id"],
        ["id", "space_id", "identity_id", "proxy_id"],
        source_schema=schema,
        referent_schema=schema,
        ondelete="RESTRICT",
    )
    op.add_column("ih_leases", sa.Column("proxy_id", sa.Uuid()), schema=schema)
    for table in ("ih_leases", "ih_lease_history"):
        op.add_column(table, sa.Column("proxy_allocation_id", sa.Uuid()), schema=schema)
    # Сохраняем выданный proxy независимо от текущей привязки; replay подключается в PA2.1.
    leases = sa.table("ih_leases", sa.column("lease_id"), sa.column("proxy_id"), schema=schema)
    history = sa.table("ih_lease_history", sa.column("lease_id"), sa.column("proxy_id"), schema=schema)
    op.execute(
        sa.update(leases).values(
            proxy_id=sa.select(history.c.proxy_id)
            .where(history.c.lease_id == leases.c.lease_id)
            .scalar_subquery()
        )
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
    for table in ("ih_leases", "ih_lease_history"):
        op.drop_column(table, "proxy_allocation_id", schema=schema)
    op.drop_column("ih_leases", "proxy_id", schema=schema)
    op.drop_constraint(
        op.f("fk_ih_bindings_allocation_id_ih_proxy_allocations"),
        "ih_bindings",
        type_="foreignkey",
        schema=schema,
    )
    op.drop_index("ix_ih_bindings_allocation_id", table_name="ih_bindings", schema=schema)
    op.drop_column("ih_bindings", "allocation_id", schema=schema)
    for name in ("reserved_count", "draining_count"):
        op.drop_constraint(
            op.f(f"ck_ih_proxies_{name}_nonnegative"), "ih_proxies", type_="check", schema=schema
        )
        op.drop_column("ih_proxies", name, schema=schema)
    op.drop_table("ih_proxy_allocations", schema=schema)
    op.drop_table("ih_proxy_groups", schema=schema)
