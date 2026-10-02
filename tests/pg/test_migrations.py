"""P0.4: миграции библиотеки совпадают с metadata и работают в отдельной схеме."""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres import metadata
from identity_hub.postgres.migrate import SCHEMA_HEAD, alembic_config, migrate

pytestmark = pytest.mark.pg


def _diff(conn: Connection) -> list[Any]:
    ctx = MigrationContext.configure(
        conn,
        opts={
            "compare_type": True,
            "compare_server_default": True,  # F4.1a: дрейф умолчаний тоже ловится (R2-D18)
            "include_object": lambda obj, name, type_, reflected, compare_to: (
                type_ != "table" or bool(name and name.startswith("ih_"))
            ),
            "version_table": "ih_alembic_version",
        },
    )
    return list(compare_metadata(ctx, metadata))


async def test_migrations_match_metadata(prepared_db: AsyncEngine) -> None:
    async with prepared_db.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == []


async def test_migrate_is_idempotent_and_uses_own_version_table(prepared_db: AsyncEngine) -> None:
    await migrate(prepared_db)
    async with prepared_db.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM ih_alembic_version"))).scalar_one()
        trgm = (await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"))).scalar()
    assert version == SCHEMA_HEAD
    assert trgm == 1


async def test_migrate_into_separate_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS ih_mig_test CASCADE"))
    await migrate(engine, db_schema="ih_mig_test")
    async with engine.connect() as conn:
        tables = set(
            (
                await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'ih_mig_test'"))
            ).scalars()
        )
    assert "ih_alembic_version" in tables
    assert set(metadata.tables) <= tables


async def test_admin_foundations_upgrade_from_0004_and_downgrade(engine: AsyncEngine) -> None:
    schema = "ih_admin_upgrade_test"
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    await migrate(engine, db_schema=schema, revision="0004")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_spaces (id, slug, title) '
                "VALUES (gen_random_uuid(), 'legacy', 'Legacy')"
            )
        )
    await migrate(engine, db_schema=schema)
    async with engine.connect() as conn:
        timezone = (
            await conn.execute(text(f"SELECT timezone FROM \"{schema}\".ih_spaces WHERE slug = 'legacy'"))
        ).scalar_one()
        version = (
            await conn.execute(text(f'SELECT version_num FROM "{schema}".ih_alembic_version'))
        ).scalar_one()
        tables = set(
            (
                await conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = :schema"),
                    {"schema": schema},
                )
            ).scalars()
        )
    assert timezone == "UTC"
    assert version == SCHEMA_HEAD
    assert {
        "ih_import_runs",
        "ih_import_items",
        "ih_provider_operations",
        "ih_stats_rollup",
        "ih_state_snapshots",
        "ih_state_snapshot_coverage",
    } <= tables

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0004"))
    async with engine.connect() as conn:
        version = (
            await conn.execute(text(f'SELECT version_num FROM "{schema}".ih_alembic_version'))
        ).scalar_one()
        timezone_exists = (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'ih_spaces' "
                    "AND column_name = 'timezone'"
                ),
                {"schema": schema},
            )
        ).scalar()
    assert version == "0004"
    assert timezone_exists is None


async def test_0014_moves_data_and_downgrades(engine: AsyncEngine) -> None:
    """F4.1a: очередь отключённого endpoint'а уходит в `paused`, числа прокси приводятся к допустимым."""
    schema = "ih_integrity_upgrade_test"
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    await migrate(engine, db_schema=schema, revision="0013")
    crypto = "'k1', ''::bytea, ''::bytea, ''::bytea"
    async with engine.begin() as conn:
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        await conn.execute(
            text("INSERT INTO ih_spaces (id, slug, title) VALUES (gen_random_uuid(), 's', 'S')")
        )
        await conn.execute(
            text(
                "INSERT INTO ih_webhook_endpoints (id, space_id, url, event_types, min_severity, enabled, "
                "failure_streak, key_id, wrapped_dek, nonce, ciphertext) "
                "SELECT gen_random_uuid(), id, 'https://x.test/' || on_, '{*}', 'info', on_, 0, "
                f"{crypto} FROM ih_spaces, (VALUES (true), (false)) v(on_)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO ih_webhook_deliveries (endpoint_id, event_id, status, attempt) "
                "SELECT e.id, gen_random_uuid(), s, 0 FROM ih_webhook_endpoints e, "
                "(VALUES ('pending'), ('failed'), ('succeeded'), ('dead')) v(s)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO ih_proxies (id, space_id, pool, scheme, host, port, fingerprint, status, "
                "error_score, fail_streak, status_source, max_identities, bound_count, key_id, wrapped_dek, "
                "nonce, ciphertext) SELECT gen_random_uuid(), id, 'p', 'socks5', 'h', 1080, 'f', 'alive', "
                f"0, 0, 'command', 0, -2, {crypto} FROM ih_spaces"
            )
        )

    await migrate(engine, db_schema=schema)

    statuses = text(
        f'SELECT e.enabled, d.status FROM "{schema}".ih_webhook_deliveries d '
        f'JOIN "{schema}".ih_webhook_endpoints e ON e.id = d.endpoint_id ORDER BY 1, 2'
    )
    async with engine.connect() as conn:
        after = [tuple(row) for row in await conn.execute(statuses)]
        proxy = (
            await conn.execute(text(f'SELECT max_identities, bound_count FROM "{schema}".ih_proxies'))
        ).one()
    assert after == [
        (False, "dead"),
        (False, "paused"),
        (False, "paused"),
        (False, "succeeded"),
        (True, "dead"),
        (True, "failed"),
        (True, "pending"),
        (True, "succeeded"),
    ]
    assert tuple(proxy) == (1, 0)

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0013"))
    async with engine.connect() as conn:
        restored = [status for _enabled, status in await conn.execute(statuses)]
        version = (
            await conn.execute(text(f'SELECT version_num FROM "{schema}".ih_alembic_version'))
        ).scalar_one()
    assert version == "0013"
    assert "paused" not in restored and restored.count("pending") == 3
