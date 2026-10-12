"""N3.1: резерв свободной ёмкости пула."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdentityCreate,
    LeaseMode,
    LeaseUnavailable,
    LeaseUnavailableReason,
    Pool,
    PoolCapacity,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = [pytest.mark.conformance]


async def _ready(space: SpaceHandle, count: int) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+reserve-{count}-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(count)
        ],
        actor="test",
    )


async def test_strict_reserve_limits_acquisition_and_capacity(space: SpaceHandle) -> None:
    await _ready(space, 3)
    pool = space.pools.define("work", service="fake_tg", reserve=1, reserve_strict=True)

    assert await pool.capacity() == 1  # значение ограничено capacity_low + 1, как и до N3.1
    assert await pool.capacity_detail() == PoolCapacity(free=3, reserved=1, available=2)
    first = await pool.acquire()
    second = await pool.acquire()
    with pytest.raises(LeaseUnavailable) as refused:
        await pool.acquire()
    assert refused.value.reason is LeaseUnavailableReason.RESERVE

    await first.release()
    await second.release()


async def test_keep_free_overrides_pool_default(space: SpaceHandle) -> None:
    await _ready(space, 3)
    pool = space.pools.define("work", service="fake_tg", reserve=0, reserve_strict=True)

    lease = await pool.acquire(keep_free=2)
    with pytest.raises(LeaseUnavailable) as refused:
        await pool.acquire(keep_free=2)
    assert refused.value.reason is LeaseUnavailableReason.RESERVE
    await lease.release()


async def test_non_strict_reserve_is_disabled_for_small_pool(space: SpaceHandle) -> None:
    await _ready(space, 2)
    pool = space.pools.define("small", service="fake_tg", reserve=2)

    first = await pool.acquire()
    second = await pool.acquire()
    assert (await pool.capacity_detail()).available == 0
    await first.release()
    await second.release()


async def test_reserve_validation(space: SpaceHandle) -> None:
    with pytest.raises(ValidationFailed):
        space.pools.define("bad", service="fake_tg", reserve=-1)
    pool = space.pools.define("ok", service="fake_tg")
    with pytest.raises(ValidationFailed):
        await pool.acquire(keep_free=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed, match="SHARED"):
        Pool(
            space=space,
            name="shared",
            service=replace(FAKE_TG, lease_mode=LeaseMode.SHARED),
            reserve=1,
        )
