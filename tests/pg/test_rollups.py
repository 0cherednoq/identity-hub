from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.events import EventDraft
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.rollups import BucketSize
from tests.fixtures.services import FAKE_EXCHANGE

pytestmark = pytest.mark.pg


async def test_postgres_rollups_are_idempotent_rebuildable_and_include_internal_events(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    before = datetime.now(UTC) - timedelta(minutes=1)
    registered = await space.register(
        service=FAKE_EXCHANGE,
        external_ref="rollup@example.test",
        credentials={"email": "rollup@example.test", "password": "test"},
        session={"cookies": {"sid": "test"}},
        lifecycle="ready",
    )
    lease = await space.pool("analytics", service=FAKE_EXCHANGE).acquire(among=[registered.identity_id])
    await lease.release()
    await hub.dispatcher.drain()
    after = datetime.now(UTC) + timedelta(minutes=1)

    lease_rows = await hub.rollups.rows(
        "tennet",
        metric="leases.acquired",
        bucket_size=BucketSize.MINUTE,
        start=before,
        end=after,
    )
    assert sum(row.value for row in lease_rows) == 1
    telemetry = await hub.rollups.rows(
        "tennet",
        metric="telemetry.lease_duration_seconds.count",
        bucket_size=BucketSize.MINUTE,
        start=before,
        end=after,
    )
    assert sum(row.value for row in telemetry) == 1

    raw = await hub.store.rollup_source_events("tennet", before, after)
    await hub.rollups.handle_events(raw)
    repeated = await hub.rollups.rows(
        "tennet",
        metric="identities.added",
        bucket_size=BucketSize.MINUTE,
        start=before,
        end=after,
    )
    assert sum(row.value for row in repeated) == 1

    async with sessionmaker.begin() as session:
        await session.execute(text("UPDATE ih_stats_rollup SET value = 999"))
    await hub.rollups.rebuild("tennet", before, after)
    rebuilt = await hub.rollups.rows(
        "tennet",
        metric="leases.acquired",
        bucket_size=BucketSize.MINUTE,
        start=before,
        end=after,
    )
    assert sum(row.value for row in rebuilt) == 1


async def test_thirty_day_series_reads_rollup_table_not_event_journal(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        plan = "\n".join(
            (
                await session.execute(
                    text(
                        "EXPLAIN SELECT bucket_start, value FROM ih_stats_rollup "
                        "WHERE space_id = gen_random_uuid() AND metric = 'identities.added' "
                        "AND bucket_size = '1d' "
                        "AND bucket_start >= now() - interval '30 days' ORDER BY bucket_start"
                    )
                )
            ).scalars()
        )
    assert "ih_events" not in plan
    assert "ih_stats_rollup" in plan


async def test_rollup_cursor_does_not_lose_late_commit(hub: IdentityHub, space: SpaceHandle) -> None:
    before = datetime.now(UTC) - timedelta(minutes=1)
    await space.ref()
    early_written = asyncio.Event()
    release_early = asyncio.Event()

    async def early() -> None:
        async with space._tx() as tx:
            await space._emit(tx, EventDraft("pool.capacity.low", actor="test", data={"n": 1}))
            early_written.set()
            await release_early.wait()

    task = asyncio.create_task(early())
    await early_written.wait()
    async with space._tx() as tx:
        await space._emit(tx, EventDraft("pool.capacity.low", actor="test", data={"n": 2}))
    await hub.dispatcher.drain()
    release_early.set()
    await task
    await hub.dispatcher.drain()

    rows = await hub.rollups.rows(
        "tennet",
        metric="events.total",
        bucket_size=BucketSize.MINUTE,
        start=before,
        end=datetime.now(UTC) + timedelta(minutes=1),
        dims={"type": "pool.capacity.low"},
    )
    assert sum(row.value for row in rows) == 2
