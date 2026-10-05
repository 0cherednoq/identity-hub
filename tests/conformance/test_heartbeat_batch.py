"""IMP-02 (I2.2b): пакетное продление аренд и координатор heartbeat (D-211)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle
from identity_hub.clock import FakeClock

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _leases(space: SpaceHandle, count: int) -> list[Any]:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg", external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(count)
        ],
        actor="test",
    )
    pool = space.pools.define("work", service="fake_tg", holder="w")
    return [await pool.acquire() for _ in range(count)]


async def test_renew_leases_extends_live_and_skips_revoked(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    first, second, revoked = await _leases(space, 3)
    await space.pools.revoke(revoked.identity_id, reason="тест", actor="admin")
    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(1)
    else:
        await asyncio.sleep(0.05)

    renewed = await hub_under_test._store.renew_leases(
        [lease._row.token for lease in (first, second, revoked)], 60.0
    )

    live = {token: renewal.expires_at for token, renewal in renewed.items() if renewal.expires_at}
    assert set(live) == {first._row.token, second._row.token}
    assert all(expires > first.expires_at for expires in live.values())
    assert renewed[revoked._row.token].expires_at is None
    for lease in (first, second):
        await lease.release()


async def test_concurrent_heartbeats_share_one_batch(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    leases = await _leases(space, 20)
    calls: list[int] = []
    store = hub_under_test._store
    original = store.renew_leases

    async def counting(tokens: Any, ttl_seconds: float) -> Any:
        calls.append(len(tokens))
        return await original(tokens, ttl_seconds)

    store.renew_leases = counting  # type: ignore[method-assign]
    try:
        results = await asyncio.gather(*(lease._extend() for lease in leases))
    finally:
        del store.renew_leases  # type: ignore[attr-defined]

    assert all(result.expires_at is not None for result in results)
    assert sum(calls) == 20 and len(calls) <= 2  # одна пачка (вторая — если первая ушла раньше)
    assert hub_under_test._heartbeats.idle  # координатор завершился вместе с очередью
    for lease in leases:
        await lease.release()


async def test_revoked_lease_in_batch_is_lost_alone(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    alive, revoked = await _leases(space, 2)
    await space.pools.revoke(revoked.identity_id, reason="тест", actor="admin")

    results = await asyncio.gather(alive._extend(), revoked._extend())

    assert results[0].expires_at is not None and results[1].expires_at is None
    await alive.release()
