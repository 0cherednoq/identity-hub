"""P0.4: миграции библиотеки совпадают с metadata и работают в отдельной схеме."""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres import metadata
from identity_hub.postgres.migrate import migrate

pytestmark = pytest.mark.pg


def _diff(conn: Connection) -> list[Any]:
    ctx = MigrationContext.configure(
        conn,
        opts={
            "compare_type": True,
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
    assert version == "0001"
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
