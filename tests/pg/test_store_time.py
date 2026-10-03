"""F4.6 (R2-D20): границы purge, корзины метрик и час снимка — по времени базы, а не процесса."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from identity_hub import IdentityHub, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.rollups import BucketSize, RollupKey, RollupRow, bucket_start
from tests.conftest import TEST_KEYS
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg

SKEW = timedelta(days=30)  # часы процесса убежали вперёд


async def _db_now(db: AsyncEngine) -> datetime:
    async with db.connect() as conn:
        value: datetime = (await conn.execute(text("SELECT now()"))).scalar_one()
    return value


@pytest.fixture
async def skewed(
    db: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[IdentityHub]:
    clock = FakeClock(await _db_now(db) + SKEW)
    hub = IdentityHub(sessionmaker, StaticSecretsProvider(TEST_KEYS, "k1"), [FAKE_TG], clock=clock)
    await hub.start()
    yield hub


async def test_minute_rollup_purge_uses_database_time(skewed: IdentityHub, db: AsyncEngine) -> None:
    ref = await skewed.space("time", "Time").ref()
    minute = bucket_start(await _db_now(db) - timedelta(hours=1), BucketSize.MINUTE, "UTC")
    await skewed._store.replace_rollups(
        [RollupRow(RollupKey(ref.id, "", "leases.acquired", (), minute, BucketSize.MINUTE), 1.0)]
    )

    assert await skewed._rollups.purge_minutes() == 0  # час назад по базе — моложе 48 часов


async def test_snapshot_hour_and_purge_use_database_time(skewed: IdentityHub, db: AsyncEngine) -> None:
    space = skewed.space("time", "Time")
    await register(space, service="fake_tg", external_ref="+1", lifecycle="ready")
    now = await _db_now(db)

    assert await skewed._state_snapshots.capture() > 0
    assert await skewed._state_snapshots.purge() == 0

    async with db.connect() as conn:
        taken = (await conn.execute(text("SELECT taken_at FROM ih_state_snapshot_coverage"))).scalar_one()
    assert taken == now.replace(minute=0, second=0, microsecond=0)


async def test_metric_minutes_use_database_time(skewed: IdentityHub, db: AsyncEngine) -> None:
    await skewed.space("time", "Time").ref()
    skewed.metrics.counter("identity_hub_custom_total", space="time")

    assert await skewed.flush_metrics() == 1

    now = await _db_now(db)
    rows = await skewed._rollups.rows(
        "time",
        metric="telemetry.custom_total",
        bucket_size=BucketSize.MINUTE,
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=5),
    )
    assert [row.value for row in rows] == [1.0]
