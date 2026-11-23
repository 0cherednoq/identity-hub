"""N4.1: GCRA-троттлинг эксклюзивной аренды и перенос TAT между держателями."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdentityCreate,
    IdentityHub,
    LeaseLost,
    LeaseMode,
    LeaseUnavailable,
    SpaceHandle,
    Throttle,
    ValidationFailed,
)
from identity_hub.clock import FakeClock
from identity_hub.store.base import StoreTx

pytestmark = [pytest.mark.conformance]


def _about_five_seconds(value: timedelta) -> bool:
    """Ровно 5 с на FakeClock; на PostgreSQL время идёт между вызовами — чуть меньше."""
    return timedelta(seconds=4.5) < value <= timedelta(seconds=5)


async def _setup(hub: IdentityHub, space: SpaceHandle) -> None:
    hub.register_service(
        replace(
            FAKE_TG,
            name="fake_throttle",
            throttles={
                "api": Throttle(2, timedelta(seconds=10)),
                "other": Throttle(2, timedelta(seconds=10)),
            },
        )
    )
    await hub.start()
    await space.identities.create(
        IdentityCreate(
            service="fake_throttle",
            external_ref="+throttle",
            lifecycle="ready",
            session={"auth_key": "k"},
        ),
        actor="test",
    )


async def test_wait_false_does_not_consume_blocked_slot(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _setup(hub_under_test, space)
    lease = await space.pools.define("work", service="fake_throttle").acquire()

    assert await lease.throttle("api", wait=False) == timedelta(0)
    first = await lease.throttle("api", wait=False)
    second = await lease.throttle("api", wait=False)
    assert _about_five_seconds(first) and _about_five_seconds(second) and second <= first
    with pytest.raises(ValidationFailed):
        await lease.throttle("missing")
    await lease.release()


async def test_state_survives_holder_change(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _setup(hub_under_test, space)
    pool = space.pools.define("work", service="fake_throttle")
    first = await pool.acquire()
    await first.throttle("api")
    await first.release()

    second = await pool.acquire()
    assert _about_five_seconds(await second.throttle("api", wait=False))
    assert await second.throttle("api") == timedelta(0)  # FakeClock сам проходит ожидание
    await second.release()


async def test_loads_throttle_once_per_used_name(
    hub_under_test: IdentityHub, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(hub_under_test, space)
    async with space._tx(own=True) as tx:
        backend = type(tx)
    original_load = backend.get_throttle_state
    original_recovery = backend.get_throttle_recovery_end
    original_save = backend.save_throttle_state
    reads: list[tuple[str, ...]] = []
    recoveries: list[UUID] = []
    writes: list[set[str]] = []

    async def load(tx: StoreTx, identity: UUID, names: Sequence[str]) -> dict[str, datetime]:
        reads.append(tuple(names))
        return await original_load(tx, identity, names)

    async def recovery(tx: StoreTx, identity: UUID) -> datetime | None:
        recoveries.append(identity)
        return await original_recovery(tx, identity)

    async def save(tx: StoreTx, identity: UUID, state: Mapping[str, datetime]) -> None:
        writes.append(set(state))
        await original_save(tx, identity, state)

    monkeypatch.setattr(backend, "get_throttle_state", load)
    monkeypatch.setattr(backend, "get_throttle_recovery_end", recovery)
    monkeypatch.setattr(backend, "save_throttle_state", save)
    pool = space.pools.define("work", service="fake_throttle")
    acquired = await pool.acquire(idempotency_key="get")
    reused = await pool.acquire(idempotency_key="get")
    assert reused.lease_id == acquired.lease_id
    await reused.release()
    assert reads == recoveries == writes == []

    work = await pool.acquire()
    first, second = await asyncio.gather(work.throttle("api", wait=False), work.throttle("api", wait=False))
    assert sorted([first, second])[0] == timedelta(0)
    assert _about_five_seconds(max(first, second))
    assert reads == [("api",)] and recoveries == [work.identity_id]
    await work.release()
    assert writes == [{"api"}]

    checking = await pool.acquire()
    assert await checking.throttle("api", wait=False) > timedelta(0)
    assert await checking.throttle("other", wait=False) == timedelta(0)
    await checking.release()
    assert reads == [("api",), ("api",), ("other",)]
    assert writes == [{"api"}, {"other"}]


async def test_release_started_during_first_throttle_prevents_a_grant(
    hub_under_test: IdentityHub, space: SpaceHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _setup(hub_under_test, space)
    async with space._tx(own=True) as tx:
        backend = type(tx)
    original = backend.get_throttle_state
    loading, resume = asyncio.Event(), asyncio.Event()

    async def load(tx: StoreTx, identity: UUID, names: Sequence[str]) -> dict[str, datetime]:
        loading.set()
        await resume.wait()
        return await original(tx, identity, names)

    monkeypatch.setattr(backend, "get_throttle_state", load)
    lease = await space.pools.define("work", service="fake_throttle").acquire()
    throttle = asyncio.create_task(lease.throttle("api", wait=False))
    try:
        await asyncio.wait_for(loading.wait(), timeout=3)
        release = asyncio.create_task(lease.release())
        await asyncio.sleep(0)  # release() закрывает локальный handle до записи в Store
        resume.set()
        with pytest.raises(LeaseLost):
            await throttle
        await release
    finally:
        resume.set()
        await asyncio.gather(throttle, return_exceptions=True)


@pytest.mark.parametrize("closed_by", ["reclaim", "maintenance", "release", "max_hold"])
async def test_expired_holder_recovers_conservative_bound(
    hub_under_test: IdentityHub, space: SpaceHandle, closed_by: str
) -> None:
    await _setup(hub_under_test, space)
    timing = {
        "ttl": timedelta(seconds=0.6),
        "heartbeat": timedelta(seconds=0.1),
        "safety_margin": timedelta(seconds=0.1),
    }
    pool = space.pools.define("work", service="fake_throttle", **timing)
    max_hold = timedelta(seconds=0.4) if closed_by == "max_hold" else None
    abandoned = await pool.acquire(max_hold=max_hold)
    await abandoned.throttle("api")
    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(timedelta(seconds=0.7))
    else:
        await asyncio.sleep(0.7)

    if closed_by == "maintenance":
        async with space._tx(own=True) as tx:
            assert await tx.expire_leases(1) == 1
    elif closed_by == "release":
        await abandoned.release()

    successor = await space.pools.define("replacement", service="fake_throttle").acquire()
    history = await space.admin.lease_history(abandoned.identity_id)
    ended = next(row for row in history if row.lease_id == abandoned.lease_id)
    assert ended.end_reason == ("max_hold" if closed_by == "max_hold" else "expired")
    removed = await hub_under_test._store.cleanup_maintenance(
        {"lease_history": timedelta(0)}, frozenset({"lease_history"}), batch_size=10
    )
    assert removed["lease_history"] == 1
    assert await successor.throttle("api", wait=False) >= timedelta(seconds=4)
    await successor.release()


@pytest.mark.parametrize("reason", ["lost", "revoked", "forced_yield", "cooperative_yield"])
async def test_recovery_distinguishes_unclean_end_from_cooperative_yield(
    hub_under_test: IdentityHub, space: SpaceHandle, reason: str
) -> None:
    await _setup(hub_under_test, space)
    background = space.pools.define("background", service="fake_throttle")
    grace = timedelta(milliseconds=100)
    urgent = space.pools.define("urgent", service="fake_throttle", priority=10, yield_grace=grace)
    victim = await background.acquire()
    assert await victim.throttle("api") == timedelta(0)
    if reason == "lost":
        await victim.release("lost")
    elif reason == "revoked":
        assert await space.pools.revoke(victim.identity_id, reason="test", actor="test")
        with pytest.raises(LeaseLost):
            await victim.throttle("other", wait=False)
    elif reason == "forced_yield":
        with pytest.raises(LeaseUnavailable):
            await urgent.acquire()
        if isinstance(hub_under_test.clock, FakeClock):
            hub_under_test.clock.advance(grace)
        else:
            await asyncio.sleep(grace.total_seconds() + 0.02)
        with pytest.raises(LeaseUnavailable):
            await urgent.acquire()
    else:
        await victim.release("yielded")
    replacement = await urgent.acquire()
    async with space._tx(own=True) as tx:
        marker = await tx.get_throttle_recovery_end(replacement.identity_id)
    assert (marker is None) == (reason == "cooperative_yield")
    assert _about_five_seconds(await replacement.throttle("api", wait=False))
    await replacement.release()


async def test_shared_holders_use_one_atomic_state(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    hub_under_test.register_service(
        replace(
            FAKE_TG,
            name="fake_shared_throttle",
            lease_mode=LeaseMode.SHARED,
            throttles={"api": Throttle(2, timedelta(seconds=10))},
        )
    )
    await hub_under_test.start()
    await space.identities.create(
        IdentityCreate(
            service="fake_shared_throttle",
            external_ref="+shared-throttle",
            lifecycle="ready",
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    pool = space.pools.define("shared", service="fake_shared_throttle")
    first, second = await pool.acquire(), await pool.acquire()

    assert await first.throttle("api", wait=False) == timedelta(0)
    assert _about_five_seconds(await second.throttle("api", wait=False))
    await first.release()
    await second.release()
