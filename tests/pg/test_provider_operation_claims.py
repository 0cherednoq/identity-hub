"""IMP-11 (I4.1): два воркера берут разные операции — claim через SKIP LOCKED (D-220)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from identity_hub import IdentityCreate
from identity_hub.hub import SpaceHandle

pytestmark = [pytest.mark.pg]


async def test_two_workers_claim_disjoint_operations(space: SpaceHandle) -> None:
    created = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1"), actor="test"
    )
    async with space._tx(own=True) as tx:
        for kind in ("login", "probe:auth"):
            await tx.enqueue_provider_operation(created.id, kind)
    first_claimed, release = asyncio.Event(), asyncio.Event()

    async def first() -> list[object]:
        async with space._tx(own=True) as tx:
            claims = await tx.claim_provider_operations("worker-a", limit=1, claim_for=timedelta(minutes=1))
            first_claimed.set()
            await release.wait()  # транзакция открыта: строка claim ещё заблокирована
            return list(claims)

    task = asyncio.create_task(first())
    await first_claimed.wait()
    async with space._tx(own=True) as tx:  # второй воркер не ждёт первого и берёт другую операцию
        second = await tx.claim_provider_operations("worker-b", limit=5, claim_for=timedelta(minutes=1))
    release.set()
    (claim_a,) = await task

    assert len(second) == 1
    assert second[0].operation_id != claim_a.operation_id  # type: ignore[attr-defined]
