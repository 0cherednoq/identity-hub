"""P7.2: sink логов аренды в ih_lease_logs (ТЗ FR-12.2)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.logging import LeaseLogPolicy, LeaseLogSink
from identity_hub.postgres.models import LeaseLogRecord
from identity_hub.postgres.store import PgTx
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg

log = logging.getLogger("worker.test")


@pytest.fixture
def sink(hub: IdentityHub) -> Iterator[LeaseLogSink]:
    hub.lease_log_sink = LeaseLogSink(LeaseLogPolicy(min_level=logging.INFO, max_per_lease=3))
    log.addHandler(hub.lease_log_sink)
    log.setLevel(logging.DEBUG)
    yield hub.lease_log_sink
    log.removeHandler(hub.lease_log_sink)


async def _logs(sm: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sm() as s:
        return list((await s.execute(select(LeaseLogRecord).order_by(LeaseLogRecord.id))).scalars())


def fast(space: SpaceHandle) -> Any:
    return space.pool(
        "p",
        service="fake_tg",
        ttl=timedelta(seconds=1.5),
        heartbeat=timedelta(seconds=0.3),
        safety_margin=timedelta(seconds=0.5),
    )


async def test_logs_flushed_with_limit_and_level(
    sink: LeaseLogSink, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    log.info("вне аренды — не пишется")
    async with fast(space).lease() as lease:
        log.debug("ниже уровня")
        log.info("первая", extra={"channel": "news"})
        await asyncio.sleep(0.6)  # heartbeat сбросил буфер
        assert [r.message for r in await _logs(sessionmaker)] == ["первая"]
        log.warning("вторая")
        log.error("третья")
        log.error("четвёртая — сверх лимита")
    rows = await _logs(sessionmaker)
    assert [r.message for r in rows] == ["первая", "вторая", "третья"]
    assert rows[0].lease_id == lease.lease_id and rows[0].identity_id == lease.identity_id
    assert rows[0].fields["channel"] == "news"
    assert rows[1].level == logging.WARNING


async def test_sink_failure_does_not_break_lease(
    sink: LeaseLogSink, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    await make_identity(space)

    async def broken(self: PgTx, rows: Any) -> None:
        raise ConnectionError("logs db down")

    monkeypatch.setattr(PgTx, "insert_lease_logs", broken)
    async with fast(space).lease() as lease:
        log.error("потеряется")
        await asyncio.sleep(0.8)
        assert not lease.lost
