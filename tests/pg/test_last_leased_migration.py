"""IMP-01 (I2.1): миграция 0020 — backfill `last_leased_at` из истории аренд, откат индекса (D-209)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres.migrate import alembic_config, migrate

pytestmark = [pytest.mark.pg]

EARLY, LATE = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_lru_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def test_backfill_takes_latest_acquisition(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0019")
    space_id, leased, fresh = uuid4(), uuid4(), uuid4()
    async with db.begin() as conn:
        await conn.execute(text(f"INSERT INTO \"{schema}\".ih_services (name, title) VALUES ('svc', 'Svc')"))
        await conn.execute(
            text(f'INSERT INTO "{schema}".ih_spaces (id, slug, title) VALUES (:id, :slug, :slug)'),
            {"id": space_id, "slug": "s"},
        )
        for identity_id, ref in ((leased, "+1"), (fresh, "+2")):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_identities (id, space_id, service, external_ref, '
                    "normalized_ref, lifecycle, standing, auth_state, availability, error_score, "
                    "fencing_seq, event_seq) VALUES (:id, :space, 'svc', :ref, :ref, 'ready', 'good', "
                    "'authenticated', 'available', 0, 0, 0)"
                ),
                {"id": identity_id, "space": space_id, "ref": ref},
            )
        for acquired_at in (EARLY, LATE):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_lease_history (lease_id, space_id, identity_id, token, '
                    "fencing, pool, holder, acquired_at, last_heartbeat_at, succeeded, failed) VALUES "
                    "(:lease, :space, :id, :token, 1, 'p', 'w', :at, :at, 0, 0)"
                ),
                {"lease": uuid4(), "space": space_id, "id": leased, "token": uuid4(), "at": acquired_at},
            )

    await migrate(db, db_schema=schema)

    async with db.connect() as conn:
        rows = dict(
            (row.id, row.last_leased_at)
            for row in await conn.execute(text(f'SELECT id, last_leased_at FROM "{schema}".ih_identities'))
        )
    assert rows == {leased: LATE, fresh: None}


async def test_downgrade_restores_previous_pick_index(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema)

    async with db.begin() as conn:
        await conn.run_sync(lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0019"))

    async with db.connect() as conn:
        definition = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = :s AND indexname = 'ix_ih_identities_pick'"
                ),
                {"s": schema},
            )
        ).scalar_one()
        column = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema = :s "
                    "AND table_name = 'ih_identities' AND column_name = 'last_leased_at'"
                ),
                {"s": schema},
            )
        ).scalar_one()
    assert "availability" in definition and column == 0
