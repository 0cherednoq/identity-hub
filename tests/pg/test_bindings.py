"""P5.2: привязка прокси к аккаунтам (ТЗ FR-8.2, 8.3, 6.7)."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import LeaseUnavailable
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import BindingRecord, EventRecord, LeaseHistoryRecord, ProxyRecord
from identity_hub.service import ProxyDownPolicy, ProxyPolicy
from tests.fixtures.services import FAKE_TG
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


async def _setup(hub: IdentityHub, name: str, policy: ProxyPolicy) -> None:
    hub.register_service(replace(FAKE_TG, name=name, proxy_policy=policy))
    await hub.start()


async def _bindings(sm: async_sessionmaker[AsyncSession]) -> dict[Any, Any]:
    async with sm() as s:
        return {b.identity_id: b.proxy_id for b in (await s.execute(select(BindingRecord))).scalars()}


async def _events(sm: async_sessionmaker[AsyncSession], event_type: str) -> list[Any]:
    async with sm() as s:
        return list((await s.execute(select(EventRecord).where(EventRecord.type == event_type))).scalars())


async def test_sticky_binding_least_loaded_and_capacity(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(hub, "tg_proxy", ProxyPolicy(pool="res", max_identities=2))
    p1, _ = await space.add_proxy("socks5://u:p@10.0.0.1:1080", pool="res")
    p2, _ = await space.add_proxy("socks5://u:p@10.0.0.2:1080", pool="res")
    ids = [await make_identity(space, service="tg_proxy") for _ in range(5)]
    pool = space.pool("p", service="tg_proxy")

    results = await asyncio.gather(*(pool.acquire() for _ in range(5)), return_exceptions=True)
    leases = [r for r in results if not isinstance(r, BaseException)]
    assert len(leases) == 4  # ёмкость 2 × 2
    assert all(isinstance(r, LeaseUnavailable) for r in results if isinstance(r, BaseException))
    load = Counter((await _bindings(sessionmaker)).values())
    assert load == {p1: 2, p2: 2}
    assert await _events(sessionmaker, "proxy.pool.exhausted")

    lease = leases[0]
    assert lease.proxy is not None and lease.proxy.startswith("socks5://u:p@10.0.0.")
    await lease.release()
    again = await pool.acquire(among=[lease.identity_id])
    assert again.proxy_id == lease.proxy_id  # sticky: тот же прокси
    async with sessionmaker() as s:
        history = (
            await s.execute(select(LeaseHistoryRecord).where(LeaseHistoryRecord.lease_id == again.lease_id))
        ).scalar_one()
    assert history.proxy_id == again.proxy_id
    assert len(ids) == 5


async def test_proxy_down_wait(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(hub, "tg_wait", ProxyPolicy(pool="res", on_proxy_down=ProxyDownPolicy.WAIT))
    proxy_id, _ = await space.add_proxy("http://10.0.0.1:80", pool="res")
    await space.add_proxy("http://10.0.0.2:80", pool="res")
    identity_id = await make_identity(space, service="tg_wait")
    pool = space.pool("p", service="tg_wait")
    lease = await pool.acquire()
    await lease.release()
    bound = (await _bindings(sessionmaker))[identity_id]
    async with sessionmaker() as s, s.begin():
        await s.execute(update(ProxyRecord).where(ProxyRecord.id == bound).values(status="quarantined"))
    with pytest.raises(LeaseUnavailable):
        await pool.acquire()  # аккаунт ждёт своего прокси
    assert proxy_id in {bound, proxy_id}


async def test_proxy_down_rebind(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(hub, "tg_rebind", ProxyPolicy(pool="res", on_proxy_down=ProxyDownPolicy.REBIND))
    await space.add_proxy("http://10.0.0.1:80", pool="res")
    await space.add_proxy("http://10.0.0.2:80", pool="res")
    identity_id = await make_identity(space, service="tg_rebind")
    pool = space.pool("p", service="tg_rebind")
    first = await pool.acquire()
    await first.release()
    async with sessionmaker() as s, s.begin():
        await s.execute(update(ProxyRecord).where(ProxyRecord.id == first.proxy_id).values(status="dead"))
    second = await pool.acquire()
    assert second.proxy_id != first.proxy_id
    assert (await _bindings(sessionmaker))[identity_id] == second.proxy_id
    reasons = [e.data["reason"] for e in await _events(sessionmaker, "proxy.binding.changed")]
    assert reasons == ["initial", "rebind"]


async def test_non_sticky_does_not_bind(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(hub, "tg_free", ProxyPolicy(pool="res", sticky=False))
    await space.add_proxy("http://10.0.0.1:80", pool="res")
    await make_identity(space, service="tg_free")
    lease = await space.pool("p", service="tg_free").acquire()
    assert lease.proxy == "http://10.0.0.1:80"
    assert await _bindings(sessionmaker) == {}


async def test_capacity_released_on_rebind_and_delete(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(hub, "tg_cap", ProxyPolicy(pool="res", on_proxy_down=ProxyDownPolicy.REBIND))
    await space.add_proxy("http://10.0.0.1:80", pool="res")
    await space.add_proxy("http://10.0.0.2:80", pool="res")
    identity_id = await make_identity(space, service="tg_cap")
    pool = space.pool("p", service="tg_cap")
    first = await pool.acquire()
    await first.release()
    async with sessionmaker() as s, s.begin():
        await s.execute(update(ProxyRecord).where(ProxyRecord.id == first.proxy_id).values(status="dead"))
    second = await pool.acquire()
    await second.release()

    async def counts() -> dict[Any, int]:
        async with sessionmaker() as s:
            return {p.id: p.bound_count for p in (await s.execute(select(ProxyRecord))).scalars()}

    assert await counts() == {first.proxy_id: 0, second.proxy_id: 1}
    await space.delete(identity_id)
    assert await counts() == {first.proxy_id: 0, second.proxy_id: 0}
