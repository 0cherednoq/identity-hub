"""N4.1: GCRA-троттлинг эксклюзивной аренды и перенос TAT между держателями."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle, Throttle, ValidationFailed

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _setup(hub: IdentityHub, space: SpaceHandle) -> None:
    hub.register_service(
        replace(
            FAKE_TG,
            name="fake_throttle",
            throttles={"api": Throttle(2, timedelta(seconds=10))},
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
    assert first == second == timedelta(seconds=5)
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
    assert await second.throttle("api", wait=False) == timedelta(seconds=5)
    assert await second.throttle("api") == timedelta(0)  # FakeClock сам проходит ожидание
    await second.release()
