"""initial schema v1

Revision ID: 0001
Revises:
Create Date: 2026-09-26 20:43:52.807819
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from identity_hub.postgres.models import XID8

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "ih_event_cursors",
        sa.Column("consumer", sa.String(length=128), nullable=False),
        sa.Column("last_tx_id", XID8(), server_default=sa.text("'0'::xid8"), nullable=False),
        sa.Column("last_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("consumer", name=op.f("pk_ih_event_cursors")),
    )
    op.create_table(
        "ih_lease_history",
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.Uuid(), nullable=False),
        sa.Column("fencing", sa.BigInteger(), nullable=False),
        sa.Column("pool", sa.String(length=128), nullable=False),
        sa.Column("holder", sa.String(length=256), nullable=False),
        sa.Column("proxy_id", sa.Uuid(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "last_heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "duration", sa.Interval(), sa.Computed("released_at - acquired_at", persisted=True), nullable=True
        ),
        sa.Column("end_reason", sa.String(length=16), nullable=True),
        sa.Column(
            "operations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("succeeded", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN ('released', 'failed', 'lost', 'expired', 'revoked')",
            name=op.f("ck_ih_lease_history_end_reason_valid"),
        ),
        sa.PrimaryKeyConstraint("lease_id", name=op.f("pk_ih_lease_history")),
        sa.UniqueConstraint("token", name=op.f("uq_ih_lease_history_token")),
    )
    op.create_index(
        "ix_ih_lease_history_identity", "ih_lease_history", ["identity_id", "acquired_at"], unique=False
    )
    op.create_index(
        "ix_ih_lease_history_open",
        "ih_lease_history",
        ["last_heartbeat_at"],
        unique=False,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_ih_lease_history_space_acquired", "ih_lease_history", ["space_id", "acquired_at"], unique=False
    )
    op.create_table(
        "ih_lease_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_lease_logs")),
    )
    op.create_index("ix_ih_lease_logs_identity_ts", "ih_lease_logs", ["identity_id", "ts"], unique=False)
    op.create_index("ix_ih_lease_logs_lease", "ih_lease_logs", ["lease_id", "id"], unique=False)
    op.create_index("ix_ih_lease_logs_ts", "ih_lease_logs", ["ts"], unique=False)
    op.create_table(
        "ih_services",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_ih_services")),
    )
    op.create_table(
        "ih_spaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_spaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_ih_spaces_slug")),
    )
    op.create_table(
        "ih_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("tx_id", XID8(), server_default=sa.text("pg_current_xact_id()"), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=True),
        sa.Column("identity_id", sa.Uuid(), nullable=True),
        sa.Column("proxy_id", sa.Uuid(), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("external", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')", name=op.f("ck_ih_events_severity_valid")
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["ih_spaces.id"], name=op.f("fk_ih_events_space_id_ih_spaces"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_events")),
        sa.UniqueConstraint("event_id", name=op.f("uq_ih_events_event_id")),
    )
    op.create_index(
        "ix_ih_events_cursor",
        "ih_events",
        ["tx_id", "id"],
        unique=False,
        postgresql_where=sa.text("external"),
    )
    op.create_index("ix_ih_events_identity", "ih_events", ["identity_id", "occurred_at"], unique=False)
    op.create_index("ix_ih_events_space_occurred", "ih_events", ["space_id", "occurred_at"], unique=False)
    op.create_index("ix_ih_events_type", "ih_events", ["space_id", "type", "occurred_at"], unique=False)
    op.create_table(
        "ih_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("external_ref", sa.String(length=256), nullable=False),
        sa.Column("lifecycle", sa.String(length=16), nullable=False),
        sa.Column("standing", sa.String(length=16), nullable=False),
        sa.Column("standing_code", sa.String(length=64), nullable=True),
        sa.Column("standing_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auth_state", sa.String(length=24), nullable=False),
        sa.Column("availability", sa.String(length=16), nullable=False),
        sa.Column("availability_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "tags", postgresql.ARRAY(sa.String(length=64)), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "profile",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("search_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "verified_factors",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("error_score", sa.Float(), nullable=False),
        sa.Column("fencing_seq", sa.BigInteger(), nullable=False),
        sa.Column("event_seq", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "auth_state IN ('no_session', 'logging_in', 'challenge', 'authenticated', 'session_invalid')",
            name=op.f("ck_ih_identities_auth_state_valid"),
        ),
        sa.CheckConstraint(
            "availability IN ('available', 'paused', 'cooling', 'quarantined')",
            name=op.f("ck_ih_identities_availability_valid"),
        ),
        sa.CheckConstraint(
            "lifecycle IN ('provisioning', 'ready', 'retired')", name=op.f("ck_ih_identities_lifecycle_valid")
        ),
        sa.CheckConstraint(
            "standing IN ('good', 'limited', 'frozen', 'appealing', 'banned', 'deleted')",
            name=op.f("ck_ih_identities_standing_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["service"],
            ["ih_services.name"],
            name=op.f("fk_ih_identities_service_ih_services"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["ih_spaces.id"],
            name=op.f("fk_ih_identities_space_id_ih_spaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_identities")),
        sa.UniqueConstraint("id", "space_id", name="uq_ih_identities_id_space"),
        sa.UniqueConstraint("service", "external_ref", name="uq_ih_identities_service_ref"),
    )
    op.create_index(
        "ix_ih_identities_availability_until",
        "ih_identities",
        ["availability_until"],
        unique=False,
        postgresql_where=sa.text("availability_until IS NOT NULL"),
    )
    op.create_index(
        "ix_ih_identities_labels",
        "ih_identities",
        ["labels"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"labels": "jsonb_path_ops"},
    )
    op.create_index("ix_ih_identities_list", "ih_identities", ["space_id", "created_at", "id"], unique=False)
    op.create_index(
        "ix_ih_identities_pick",
        "ih_identities",
        ["space_id", "service", "availability"],
        unique=False,
        postgresql_where=sa.text(
            "lifecycle = 'ready' AND auth_state = 'authenticated' AND standing IN ('good', 'limited')"
        ),
    )
    op.create_index(
        "ix_ih_identities_profile",
        "ih_identities",
        ["profile"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"profile": "jsonb_path_ops"},
    )
    op.create_index(
        "ix_ih_identities_regions",
        "ih_identities",
        ["space_id", "service", "lifecycle", "standing", "auth_state"],
        unique=False,
    )
    op.create_index(
        "ix_ih_identities_search",
        "ih_identities",
        ["search_text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_ih_identities_standing_until",
        "ih_identities",
        ["standing_until"],
        unique=False,
        postgresql_where=sa.text("standing_until IS NOT NULL"),
    )
    op.create_index("ix_ih_identities_tags", "ih_identities", ["tags"], unique=False, postgresql_using="gin")
    op.create_index(
        "ix_ih_identities_verified",
        "ih_identities",
        ["verified_factors"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "ih_notification_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("kind IN ('telegram')", name=op.f("ck_ih_notification_channels_kind_valid")),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["ih_spaces.id"],
            name=op.f("fk_ih_notification_channels_space_id_ih_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_notification_channels")),
    )
    op.create_table(
        "ih_proxies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("pool", sa.String(length=128), nullable=False),
        sa.Column("scheme", sa.String(length=16), nullable=False),
        sa.Column("host", sa.String(length=256), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_score", sa.Float(), nullable=False),
        sa.Column("fail_streak", sa.Integer(), nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status_changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("status_source", sa.String(length=16), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pinned_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_identities", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "status IN ('alive', 'quarantined', 'dead')", name=op.f("ck_ih_proxies_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["space_id"], ["ih_spaces.id"], name=op.f("fk_ih_proxies_space_id_ih_spaces"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_proxies")),
        sa.UniqueConstraint("id", "space_id", name="uq_ih_proxies_id_space"),
        sa.UniqueConstraint("space_id", "fingerprint", name="uq_ih_proxies_space_fingerprint"),
    )
    op.create_index(
        "ix_ih_proxies_host",
        "ih_proxies",
        ["host"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"host": "gin_trgm_ops"},
    )
    op.create_index("ix_ih_proxies_pick", "ih_proxies", ["space_id", "pool", "status"], unique=False)
    op.create_table(
        "ih_webhook_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("event_types", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column(
            "filter",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("min_severity", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("failure_streak", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "min_severity IN ('info', 'warning', 'critical')",
            name=op.f("ck_ih_webhook_endpoints_min_severity_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["ih_spaces.id"],
            name=op.f("fk_ih_webhook_endpoints_space_id_ih_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_webhook_endpoints")),
    )
    op.create_table(
        "ih_bindings",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("proxy_id", sa.Uuid(), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_bindings_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proxy_id", "space_id"],
            ["ih_proxies.id", "ih_proxies.space_id"],
            name=op.f("fk_ih_bindings_proxy_id_ih_proxies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("identity_id", name=op.f("pk_ih_bindings")),
    )
    op.create_index("ix_ih_bindings_proxy_id", "ih_bindings", ["proxy_id"], unique=False)
    op.create_table(
        "ih_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("hint", sa.Text(), nullable=False),
        sa.Column("context_key_id", sa.String(length=64), nullable=False),
        sa.Column("context_wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("context_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("context_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("answer_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("answer_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_challenges_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_challenges")),
    )
    op.create_index(
        "uq_ih_challenges_open",
        "ih_challenges",
        ["identity_id"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_table(
        "ih_credentials",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_credentials_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", name=op.f("pk_ih_credentials")),
    )
    op.create_table(
        "ih_identity_data",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_identity_data_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", "namespace", name=op.f("pk_ih_identity_data")),
    )
    op.create_index(
        "ix_ih_identity_data_namespace", "ih_identity_data", ["space_id", "namespace"], unique=False
    )
    op.create_table(
        "ih_identity_links",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("linked_identity_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("identity_id <> linked_identity_id", name=op.f("ck_ih_identity_links_not_self")),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_identity_links_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["linked_identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_identity_links_linked_identity_id_ih_identities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("identity_id", "role", name=op.f("pk_ih_identity_links")),
    )
    op.create_index("ix_ih_identity_links_linked", "ih_identity_links", ["linked_identity_id"], unique=False)
    op.create_table(
        "ih_leases",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.Uuid(), nullable=False),
        sa.Column("fencing", sa.BigInteger(), nullable=False),
        sa.Column("pool", sa.String(length=128), nullable=False),
        sa.Column("holder", sa.String(length=256), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_leases_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", name=op.f("pk_ih_leases")),
        sa.UniqueConstraint("lease_id", name=op.f("uq_ih_leases_lease_id")),
        sa.UniqueConstraint("token", name=op.f("uq_ih_leases_token")),
    )
    op.create_index("ix_ih_leases_expires_at", "ih_leases", ["expires_at"], unique=False)
    op.create_index(
        "uq_ih_leases_idempotency",
        "ih_leases",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "ih_notification_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("event_types", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("min_severity", sa.String(length=16), nullable=False),
        sa.Column(
            "filter",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("digest_seconds", sa.Integer(), nullable=True),
        sa.Column("template", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=256), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "min_severity IN ('info', 'warning', 'critical')",
            name=op.f("ck_ih_notification_rules_min_severity_valid"),
        ),
        sa.CheckConstraint(
            "digest_seconds IS NULL OR digest_seconds BETWEEN 10 AND 86400",
            name=op.f("ck_ih_notification_rules_digest_range"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["ih_notification_channels.id"],
            name=op.f("fk_ih_notification_rules_channel_id_ih_notification_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["ih_spaces.id"],
            name=op.f("fk_ih_notification_rules_space_id_ih_spaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_notification_rules")),
    )
    op.create_index(
        "ix_ih_notification_rules_space",
        "ih_notification_rules",
        ["space_id"],
        unique=False,
        postgresql_where=sa.text("enabled"),
    )
    op.create_table(
        "ih_operation_keys",
        sa.Column("key", sa.String(length=256), nullable=False),
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_operation_keys_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_ih_operation_keys")),
    )
    op.create_index("ix_ih_operation_keys_created_at", "ih_operation_keys", ["created_at"], unique=False)
    op.create_table(
        "ih_quota_usage",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_quota_usage_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "identity_id", "kind", "window_seconds", "window_start", name=op.f("pk_ih_quota_usage")
        ),
    )
    op.create_index("ix_ih_quota_usage_window_start", "ih_quota_usage", ["window_start"], unique=False)
    op.create_table(
        "ih_region_states",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("change_source", sa.String(length=16), nullable=False),
        sa.Column("changed_by", sa.String(length=256), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_status", sa.String(length=128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_observation", sa.String(length=32), nullable=True),
        sa.Column("pinned_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "change_source <> 'override' OR change_reason IS NOT NULL",
            name=op.f("ck_ih_region_states_override_has_reason"),
        ),
        sa.CheckConstraint(
            "change_source IN ('command', 'probe', 'error', 'webhook', 'override', 'timer', 'import')",
            name=op.f("ck_ih_region_states_change_source_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_region_states_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", "region", "key", name=op.f("pk_ih_region_states")),
    )
    op.create_index(
        "ix_ih_region_states_pinned",
        "ih_region_states",
        ["pinned_until"],
        unique=False,
        postgresql_where=sa.text("pinned_until IS NOT NULL"),
    )
    op.create_index(
        "ix_ih_region_states_state", "ih_region_states", ["space_id", "region", "key", "state"], unique=False
    )
    op.create_index("ix_ih_region_states_synced", "ih_region_states", ["region", "synced_at"], unique=False)
    op.create_table(
        "ih_sessions",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.String(length=64), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("written_by_fencing", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "status IN ('valid', 'invalid', 'expired')", name=op.f("ck_ih_sessions_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_sessions_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", "slot", name=op.f("pk_ih_sessions")),
    )
    op.create_index(
        "ix_ih_sessions_expires_at",
        "ih_sessions",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_table(
        "ih_verifications",
        sa.Column("identity_id", sa.Uuid(), nullable=False),
        sa.Column("factor", sa.String(length=64), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("via_identity_id", sa.Uuid(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("evidence_key_id", sa.String(length=64), nullable=True),
        sa.Column("evidence_wrapped_dek", sa.LargeBinary(), nullable=True),
        sa.Column("evidence_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("evidence_ciphertext", sa.LargeBinary(), nullable=True),
        sa.ForeignKeyConstraint(
            ["identity_id", "space_id"],
            ["ih_identities.id", "ih_identities.space_id"],
            name=op.f("fk_ih_verifications_identity_id_ih_identities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("identity_id", "factor", name=op.f("pk_ih_verifications")),
    )
    op.create_index(
        "ix_ih_verifications_expires_at",
        "ih_verifications",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_table(
        "ih_webhook_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_ih_webhook_deliveries_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["ih_webhook_endpoints.id"],
            name=op.f("fk_ih_webhook_deliveries_endpoint_id_ih_webhook_endpoints"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_webhook_deliveries")),
        sa.UniqueConstraint("endpoint_id", "event_id", name="uq_ih_webhook_deliveries_endpoint_event"),
    )
    op.create_index(
        "ix_ih_webhook_deliveries_due",
        "ih_webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.create_table(
        "ih_notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.Column("digest_key", sa.String(length=128), nullable=True),
        sa.Column("event_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_ih_notifications_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["ih_notification_rules.id"],
            name=op.f("fk_ih_notifications_rule_id_ih_notification_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ih_notifications")),
    )
    op.create_index(
        "ix_ih_notifications_due",
        "ih_notifications",
        ["scheduled_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.create_index(
        "uq_ih_notifications_open_digest",
        "ih_notifications",
        ["digest_key"],
        unique=True,
        postgresql_where=sa.text("digest_key IS NOT NULL AND status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ih_notifications_open_digest",
        table_name="ih_notifications",
        postgresql_where=sa.text("digest_key IS NOT NULL AND status = 'pending'"),
    )
    op.drop_index(
        "ix_ih_notifications_due",
        table_name="ih_notifications",
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.drop_table("ih_notifications")
    op.drop_index(
        "ix_ih_webhook_deliveries_due",
        table_name="ih_webhook_deliveries",
        postgresql_where=sa.text("status IN ('pending', 'failed')"),
    )
    op.drop_table("ih_webhook_deliveries")
    op.drop_index(
        "ix_ih_verifications_expires_at",
        table_name="ih_verifications",
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.drop_table("ih_verifications")
    op.drop_index(
        "ix_ih_sessions_expires_at",
        table_name="ih_sessions",
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.drop_table("ih_sessions")
    op.drop_index("ix_ih_region_states_synced", table_name="ih_region_states")
    op.drop_index("ix_ih_region_states_state", table_name="ih_region_states")
    op.drop_index(
        "ix_ih_region_states_pinned",
        table_name="ih_region_states",
        postgresql_where=sa.text("pinned_until IS NOT NULL"),
    )
    op.drop_table("ih_region_states")
    op.drop_index("ix_ih_quota_usage_window_start", table_name="ih_quota_usage")
    op.drop_table("ih_quota_usage")
    op.drop_index("ix_ih_operation_keys_created_at", table_name="ih_operation_keys")
    op.drop_table("ih_operation_keys")
    op.drop_index(
        "ix_ih_notification_rules_space",
        table_name="ih_notification_rules",
        postgresql_where=sa.text("enabled"),
    )
    op.drop_table("ih_notification_rules")
    op.drop_index(
        "uq_ih_leases_idempotency",
        table_name="ih_leases",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_index("ix_ih_leases_expires_at", table_name="ih_leases")
    op.drop_table("ih_leases")
    op.drop_index("ix_ih_identity_links_linked", table_name="ih_identity_links")
    op.drop_table("ih_identity_links")
    op.drop_index("ix_ih_identity_data_namespace", table_name="ih_identity_data")
    op.drop_table("ih_identity_data")
    op.drop_table("ih_credentials")
    op.drop_index(
        "uq_ih_challenges_open", table_name="ih_challenges", postgresql_where=sa.text("resolved_at IS NULL")
    )
    op.drop_table("ih_challenges")
    op.drop_index("ix_ih_bindings_proxy_id", table_name="ih_bindings")
    op.drop_table("ih_bindings")
    op.drop_table("ih_webhook_endpoints")
    op.drop_index("ix_ih_proxies_pick", table_name="ih_proxies")
    op.drop_index(
        "ix_ih_proxies_host",
        table_name="ih_proxies",
        postgresql_using="gin",
        postgresql_ops={"host": "gin_trgm_ops"},
    )
    op.drop_table("ih_proxies")
    op.drop_table("ih_notification_channels")
    op.drop_index("ix_ih_identities_verified", table_name="ih_identities", postgresql_using="gin")
    op.drop_index("ix_ih_identities_tags", table_name="ih_identities", postgresql_using="gin")
    op.drop_index(
        "ix_ih_identities_standing_until",
        table_name="ih_identities",
        postgresql_where=sa.text("standing_until IS NOT NULL"),
    )
    op.drop_index(
        "ix_ih_identities_search",
        table_name="ih_identities",
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.drop_index("ix_ih_identities_regions", table_name="ih_identities")
    op.drop_index(
        "ix_ih_identities_profile",
        table_name="ih_identities",
        postgresql_using="gin",
        postgresql_ops={"profile": "jsonb_path_ops"},
    )
    op.drop_index(
        "ix_ih_identities_pick",
        table_name="ih_identities",
        postgresql_where=sa.text(
            "lifecycle = 'ready' AND auth_state = 'authenticated' AND standing IN ('good', 'limited')"
        ),
    )
    op.drop_index("ix_ih_identities_list", table_name="ih_identities")
    op.drop_index(
        "ix_ih_identities_labels",
        table_name="ih_identities",
        postgresql_using="gin",
        postgresql_ops={"labels": "jsonb_path_ops"},
    )
    op.drop_index(
        "ix_ih_identities_availability_until",
        table_name="ih_identities",
        postgresql_where=sa.text("availability_until IS NOT NULL"),
    )
    op.drop_table("ih_identities")
    op.drop_index("ix_ih_events_type", table_name="ih_events")
    op.drop_index("ix_ih_events_space_occurred", table_name="ih_events")
    op.drop_index("ix_ih_events_identity", table_name="ih_events")
    op.drop_index("ix_ih_events_cursor", table_name="ih_events", postgresql_where=sa.text("external"))
    op.drop_table("ih_events")
    op.drop_table("ih_spaces")
    op.drop_table("ih_services")
    op.drop_index("ix_ih_lease_logs_ts", table_name="ih_lease_logs")
    op.drop_index("ix_ih_lease_logs_lease", table_name="ih_lease_logs")
    op.drop_index("ix_ih_lease_logs_identity_ts", table_name="ih_lease_logs")
    op.drop_table("ih_lease_logs")
    op.drop_index("ix_ih_lease_history_space_acquired", table_name="ih_lease_history")
    op.drop_index(
        "ix_ih_lease_history_open",
        table_name="ih_lease_history",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.drop_index("ix_ih_lease_history_identity", table_name="ih_lease_history")
    op.drop_table("ih_lease_history")
    op.drop_table("ih_event_cursors")
