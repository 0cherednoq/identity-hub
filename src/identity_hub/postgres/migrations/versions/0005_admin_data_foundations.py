"""admin data foundations (D-133)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str | None:
    schema: str | None = op.get_context().version_table_schema
    return schema


def upgrade() -> None:
    schema = _schema()
    op.add_column(
        "ih_spaces",
        sa.Column("timezone", sa.String(length=64), server_default=sa.text("'UTC'"), nullable=False),
        schema=schema,
    )
    op.create_table(
        "ih_import_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("source", sa.String(length=256), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("on_existing", sa.String(length=24), server_default=sa.text("'skip'"), nullable=False),
        sa.Column("verify_sessions", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("processed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "report",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total >= 0 AND processed >= 0 AND created >= 0 AND updated >= 0 "
            "AND skipped >= 0 AND failed >= 0",
            name=op.f("ck_ih_import_runs_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "on_existing IN ('skip', 'update_session', 'fail')",
            name=op.f("ck_ih_import_runs_on_existing_valid"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelling', 'cancelled')",
            name=op.f("ck_ih_import_runs_state_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["service"],
            ["ih_services.name"],
            name=op.f("fk_ih_import_runs_service_ih_services"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["ih_spaces.id"],
            name=op.f("fk_ih_import_runs_space_id_ih_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_import_runs")),
        sa.UniqueConstraint("id", "space_id", name="uq_ih_import_runs_id_space"),
        schema=schema,
    )
    op.create_index(
        "ix_ih_import_runs_active",
        "ih_import_runs",
        ["updated_at"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("state IN ('pending', 'running', 'cancelling')"),
    )
    op.create_index(
        "ix_ih_import_runs_space_started",
        "ih_import_runs",
        ["space_id", "started_at"],
        unique=False,
        schema=schema,
    )
    op.create_table(
        "ih_import_items",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.BigInteger(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("claim_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_by", sa.String(length=256), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_ih_import_items_position_nonnegative")),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'skipped', 'failed')",
            name=op.f("ck_ih_import_items_state_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "space_id"],
            ["ih_import_runs.id", "ih_import_runs.space_id"],
            name=op.f("fk_ih_import_items_run_id_ih_import_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "position", name=op.f("pk_ih_import_items")),
        schema=schema,
    )
    op.create_index(
        "ix_ih_import_items_claim",
        "ih_import_items",
        ["run_id", "position"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("state IN ('pending', 'running')"),
    )
    op.create_table(
        "ih_provider_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=True),
        sa.Column("claim_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_by", sa.String(length=256), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt >= 0 AND claim_token >= 0",
            name=op.f("ck_ih_provider_operations_counters_nonnegative"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'retryable', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_ih_provider_operations_state_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_provider_operations_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_provider_operations")),
        schema=schema,
    )
    op.create_index(
        "ix_ih_provider_operations_claim",
        "ih_provider_operations",
        ["available_at"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("state IN ('pending', 'retryable')"),
    )
    op.create_index(
        "ix_ih_provider_operations_identity",
        "ih_provider_operations",
        ["identity_id", "created_at"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_ih_provider_operations_stale",
        "ih_provider_operations",
        ["claim_expires_at"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index(
        "ix_ih_lease_history_space_released",
        "ih_lease_history",
        ["space_id", "released_at"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_ih_lease_history_space_holder_acquired",
        "ih_lease_history",
        ["space_id", "holder", "acquired_at"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_ih_events_identity_type_occurred",
        "ih_events",
        ["identity_id", "type", "occurred_at"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_ih_events_identity_type_occurred", table_name="ih_events", schema=schema)
    op.drop_index("ix_ih_lease_history_space_holder_acquired", table_name="ih_lease_history", schema=schema)
    op.drop_index("ix_ih_lease_history_space_released", table_name="ih_lease_history", schema=schema)
    op.drop_table("ih_provider_operations", schema=schema)
    op.drop_table("ih_import_items", schema=schema)
    op.drop_table("ih_import_runs", schema=schema)
    op.drop_column("ih_spaces", "timezone", schema=schema)
