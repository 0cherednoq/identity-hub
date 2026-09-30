"""P0.2: схема v1 создаётся на PostgreSQL 16 целиком, с ограничениями и индексами ТЗ §8.5."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres import metadata, schema_options

pytestmark = pytest.mark.pg

EXPECTED_TABLES = {
    "ih_spaces", "ih_services",
    "ih_identities", "ih_region_states", "ih_identity_links", "ih_identity_data",
    "ih_credentials", "ih_sessions", "ih_challenges", "ih_verifications",
    "ih_leases", "ih_lease_history", "ih_lease_logs",
    "ih_quota_usage", "ih_operation_keys",
    "ih_proxies", "ih_bindings",
    "ih_events", "ih_event_cursors",
    "ih_webhook_endpoints", "ih_webhook_deliveries",
    "ih_notification_channels", "ih_notification_rules", "ih_notifications",
    "ih_import_runs", "ih_import_items", "ih_provider_operations",
    "ih_stats_rollup", "ih_stats_metric_batches",
    "ih_state_snapshots", "ih_state_snapshot_coverage",
}  # fmt: skip

EXPECTED_INDEXES = {
    "ix_ih_identities_pick",
    "ix_ih_identities_tags",
    "ix_ih_identities_verified",
    "ix_ih_identities_labels",
    "ix_ih_identities_profile",
    "ix_ih_identities_search",
    "ix_ih_identities_list",
    "ix_ih_identities_availability_until",
    "ix_ih_identities_standing_until",
    "ix_ih_region_states_synced",
    "ix_ih_region_states_pinned",
    "ix_ih_events_cursor",
    "ix_ih_proxies_host",
    "ix_ih_webhook_deliveries_due",
    "ix_ih_notifications_due",
    "uq_ih_challenges_open",
    "uq_ih_leases_idempotency",
    "ix_ih_import_runs_active",
    "ix_ih_import_items_claim",
    "ix_ih_provider_operations_claim",
    "ix_ih_lease_history_space_released",
    "ix_ih_lease_history_space_holder_acquired",
    "ix_ih_events_identity_type_occurred",
    "ix_ih_stats_rollup_series",
    "ix_ih_stats_rollup_minute_retention",
    "ix_ih_state_snapshots_series",
}


async def _create_in(engine: AsyncEngine, schema: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn = await conn.execution_options(**schema_options(schema))
        await conn.run_sync(metadata.create_all)


def test_metadata_has_all_tables() -> None:
    assert set(metadata.tables) == EXPECTED_TABLES


async def test_create_all_in_separate_schema(engine: AsyncEngine) -> None:
    await _create_in(engine, "ih_schema_test")
    async with engine.connect() as conn:
        tables = set(
            (
                await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'ih_schema_test'")
                )
            ).scalars()
        )
        indexes = set(
            (
                await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'ih_schema_test'")
                )
            ).scalars()
        )
        checks = set(
            (
                await conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                        "WHERE n.nspname = 'ih_schema_test' AND c.contype = 'c'"
                    )
                )
            ).scalars()
        )
    assert tables == EXPECTED_TABLES
    assert indexes >= EXPECTED_INDEXES
    assert "ck_ih_region_states_override_has_reason" in checks
    assert "ck_ih_identity_links_not_self" in checks


async def test_lease_history_duration_is_generated(engine: AsyncEngine) -> None:
    await _create_in(engine, "ih_schema_gen")
    async with engine.connect() as conn:
        generated = (
            await conn.execute(
                text(
                    "SELECT is_generated FROM information_schema.columns "
                    "WHERE table_schema = 'ih_schema_gen' AND table_name = 'ih_lease_history' "
                    "AND column_name = 'duration'"
                )
            )
        ).scalar_one()
    assert generated == "ALWAYS"
