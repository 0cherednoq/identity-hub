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
from tests.fixtures.api import add_proxy
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
    await _setup(hub, "tg_proxy", ProxyPolicy(group="res", max_identities=2))
    p1, _ = await add_proxy(space, "socks5://u:p@10.0.0.1:1080", pool="res")
    p2, _ = await add_proxy(space, "socks5://u:p@10.0.0.2:1080", pool="res")
    ids = [await make_identity(space, service="tg_proxy") for _ in range(5)]
    pool = space.pools.define("p", service="tg_proxy")

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
    await _setup(hub, "tg_wait", ProxyPolicy(group="res", on_proxy_down=ProxyDownPolicy.WAIT))
    proxy_id, _ = await add_proxy(space, "http://10.0.0.1:80", pool="res")
    await add_proxy(space, "http://10.0.0.2:80", pool="res")
    identity_id = await make_identity(space, service="tg_wait")
    pool = space.pools.define("p", service="tg_wait")
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
    await _setup(hub, "tg_rebind", ProxyPolicy(group="res", on_proxy_down=ProxyDownPolicy.REBIND))
    await add_proxy(space, "http://10.0.0.1:80", pool="res")
    await add_proxy(space, "http://10.0.0.2:80", pool="res")
    identity_id = await make_identity(space, service="tg_rebind")
    pool = space.pools.define("p", service="tg_rebind")
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
    await _setup(hub, "tg_free", ProxyPolicy(group="res", sticky=False))
    await add_proxy(space, "http://10.0.0.1:80", pool="res")
    await make_identity(space, service="tg_free")
    lease = await space.pools.define("p", service="tg_free").acquire()
    assert lease.proxy == "http://10.0.0.1:80"
    assert await _bindings(sessionmaker) == {}


async def test_capacity_released_on_rebind_and_delete(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(hub, "tg_cap", ProxyPolicy(group="res", on_proxy_down=ProxyDownPolicy.REBIND))
    await add_proxy(space, "http://10.0.0.1:80", pool="res")
    await add_proxy(space, "http://10.0.0.2:80", pool="res")
    identity_id = await make_identity(space, service="tg_cap")
    pool = space.pools.define("p", service="tg_cap")
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
    await space.identities.delete(identity_id)
    assert await counts() == {first.proxy_id: 0, second.proxy_id: 0}


async def test_manual_rebind_race_preserves_capacity_and_bound_count(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    proxy_id, _ = await add_proxy(space, "http://10.0.0.9:80", pool="manual", max_identities=1)
    await space.proxies.override(proxy_id, "alive", reason="ready for manual binding", actor="test:admin")
    identities = [await make_identity(space) for _ in range(2)]

    results = await asyncio.gather(
        *(
            space.proxies.rebind(identity_id, proxy_id, reason="manual support rebind", actor="test:operator")
            for identity_id in identities
        ),
        return_exceptions=True,
    )

    assert sum(result is True for result in results) == 1
    assert sum(isinstance(result, LeaseUnavailable) for result in results) == 1
    async with sessionmaker() as session:
        proxy = (await session.execute(select(ProxyRecord).where(ProxyRecord.id == proxy_id))).scalar_one()
        bindings = list(
            (await session.execute(select(BindingRecord).where(BindingRecord.proxy_id == proxy_id))).scalars()
        )
    assert proxy.bound_count == len(bindings) == 1
