from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import StateSnapshotCoverageRecord, StateSnapshotRecord
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_EXCHANGE

pytestmark = pytest.mark.pg


async def test_postgres_snapshot_matches_current_slice_and_two_workers_do_not_duplicate(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(2):
        await register(
            space,
            service=FAKE_EXCHANGE,
            external_ref=f"snapshot-{index}@example.test",
            credentials={"email": f"snapshot-{index}@example.test", "password": "test"},
            session={"cookies": {"sid": "test"}},
            lifecycle="ready",
        )
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    await asyncio.gather(hub.state_snapshots.capture(at=hour), hub.state_snapshots.capture(at=hour))

    frames = await hub.state_snapshots.frames("tennet", start=hour, end=hour + timedelta(hours=1))
    assert len(frames) == 1 and frames[0].complete
    assert sum(row.count for row in frames[0].rows if row.region == "standing") == 2

    async with sessionmaker() as session:
        coverage_count = (
            await session.execute(
                select(func.count())
                .select_from(StateSnapshotCoverageRecord)
                .where(StateSnapshotCoverageRecord.taken_at == hour)
            )
        ).scalar_one()
        row_count = (
            await session.execute(
                select(func.count())
                .select_from(StateSnapshotRecord)
                .where(StateSnapshotRecord.taken_at == hour)
            )
        ).scalar_one()
    assert coverage_count == 1
    assert row_count == len(frames[0].rows)


async def test_postgres_empty_space_has_complete_zero_frame(hub: IdentityHub) -> None:
    empty = hub.space("empty-snapshot")
    await empty.ref()
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    await hub.state_snapshots.backfill_current(at=hour)
    frames = await hub.state_snapshots.frames("empty-snapshot", start=hour, end=hour + timedelta(hours=1))
    assert len(frames) == 1
    assert frames[0].complete and frames[0].rows == ()
