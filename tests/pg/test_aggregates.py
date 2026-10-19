"""P8.2: агрегированные события (ТЗ FR-13.3, 13.4, D-119)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import LeaseLost, LeaseUnavailable
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord
from identity_hub.postgres.store import PgStore, PgTx
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def _events(sm: async_sessionmaker[AsyncSession], prefix: str) -> list[Any]:
    async with sm() as s:
        rows = (await s.execute(select(EventRecord).order_by(EventRecord.id))).scalars()
        return [e for e in rows if e.type.startswith(prefix)]


async def test_capacity_transitions(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for _ in range(3):
        await make_identity(space)
    pool = space.pools.define("parsers", service="fake_tg", capacity_low=1)
    assert await pool.capacity() == 2  # до threshold + 1
    space_ref = await space.ref()
    leases = [await pool.acquire(), await pool.acquire()]
    assert await pool.capacity() == 1
    assert await pool.capacity() == 1  # то же состояние — без повторного события
    await leases.pop().release()
    assert await pool.capacity() == 2
    assert await pool.capacity() == 2  # ok не дублируется
    leases.extend([await pool.acquire(), await pool.acquire()])
    with pytest.raises(LeaseUnavailable):
        await pool.acquire()  # сам проверяет ёмкость → empty
    for lease in leases:
        await lease.release()
    await pool.capacity()
    events = await _events(sessionmaker, "pool.capacity.")
    assert [(e.type, e.severity) for e in events] == [
        ("pool.capacity.low", "warning"),
        ("pool.capacity.ok", "info"),
        ("pool.capacity.empty", "critical"),
        ("pool.capacity.low", "warning"),
        ("pool.capacity.ok", "info"),
    ]
    assert events[0].data["pool"] == "parsers"
    recovered = [event for event in events if event.type == "pool.capacity.ok"]
    assert all(event.space_id == space_ref.id and event.service == "fake_tg" for event in recovered)
    assert [event.data["previous_state"] for event in recovered] == ["low", "low"]
    assert [(event.data["eligible"], event.data["free"]) for event in recovered] == [(3, 2), (3, 2)]


async def test_release_emits_capacity_ok_without_polling(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await make_identity(space)
    pool = space.pools.define("release-kick", service="fake_tg")
    lease = await pool.acquire()
    with pytest.raises(LeaseUnavailable):
        await pool.acquire()

    await lease.release()

    events = await _events(sessionmaker, "pool.capacity.")
    assert [(event.type, event.data.get("previous_state")) for event in events] == [
        ("pool.capacity.empty", None),
        ("pool.capacity.ok", "empty"),
    ]


async def test_worker_leases_lost_aggregate(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for _ in range(2):
        await make_identity(space)
    pool = space.pools.define(
        "p",
        service="fake_tg",
        holder="host:42",
        ttl=timedelta(seconds=1.5),
        heartbeat=timedelta(seconds=0.3),
        safety_margin=timedelta(seconds=0.5),
    )

    async def broken(self: PgTx, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("down")

    async def lose() -> None:
        with pytest.raises(LeaseLost):
            async with pool.lease():
                await asyncio.sleep(10)

    monkeypatch.setattr(PgTx, "extend_lease", broken)
    monkeypatch.setattr(PgStore, "renew_leases", broken)  # пакетный heartbeat (D-211)
    await asyncio.gather(lose(), lose())
    monkeypatch.undo()
    assert await hub.flush_lease_losses() == 1
    assert await hub.flush_lease_losses() == 0
    (event,) = await _events(sessionmaker, "worker.")
    assert (event.type, event.data["holder"], event.data["count"]) == ("worker.leases_lost", "host:42", 2)
