"""N3.1: параллельные захваты не съедают строгий резерв."""

from __future__ import annotations

import asyncio

import pytest

from identity_hub import IdentityCreate, LeaseUnavailable, SpaceHandle

pytestmark = [pytest.mark.pg]


async def test_parallel_acquisitions_leave_strict_reserve(space: SpaceHandle) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg",
                external_ref=f"+reserve-race-{index}",
                lifecycle="ready",
                session={"auth_key": "k"},
            )
            for index in range(6)
        ],
        actor="test",
    )
    pool = space.pools.define("race", service="fake_tg", reserve=2, reserve_strict=True)

    results = await asyncio.gather(*(pool.acquire() for _ in range(8)), return_exceptions=True)
    leases = [result for result in results if not isinstance(result, BaseException)]
    assert len(leases) <= 4
    failures = [result for result in results if isinstance(result, BaseException)]
    assert all(isinstance(result, LeaseUnavailable) for result in failures)
    assert (await pool.capacity_detail()).free >= 2
    await asyncio.gather(*(lease.release() for lease in leases))
