"""N3.2a: request_yield и release безопасны в обоих порядках."""

from __future__ import annotations

import asyncio

import pytest

from identity_hub import IdentityCreate, LeaseUnavailable, SpaceHandle

pytestmark = [pytest.mark.anyio, pytest.mark.pg]


async def _lease(space: SpaceHandle):  # type: ignore[no-untyped-def]
    await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+priority-race",
            lifecycle="ready",
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    return await space.pools.define("low", service="fake_tg", priority=0).acquire()


async def test_request_yield_racing_release_leaves_consistent_state(space: SpaceHandle) -> None:
    lease = await _lease(space)
    urgent = space.pools.define("urgent", service="fake_tg", priority=10)

    async def request() -> None:
        with pytest.raises(LeaseUnavailable):
            await urgent.acquire()

    await asyncio.gather(request(), lease.release())
    acquired = await urgent.acquire()
    assert not acquired.yield_requested
    await acquired.release()
