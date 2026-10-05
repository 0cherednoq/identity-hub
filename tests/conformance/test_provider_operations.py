"""IMP-11 (I4.1): claim, apply и recover операций провайдера (D-220)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle
from identity_hub.clock import FakeClock
from identity_hub.domain.provider_operations import RetryPolicy

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

CLAIM = timedelta(seconds=30)
SHORT = timedelta(seconds=0.4)


async def _identity(space: SpaceHandle) -> UUID:
    created = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1"), actor="test"
    )
    return created.id


async def _pass(hub: IdentityHub, seconds: float) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=seconds))
    else:
        await asyncio.sleep(seconds)


async def test_claim_apply_and_late_answer(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    async with space._tx(own=True) as tx:
        queued = await tx.enqueue_provider_operation(identity_id, "login")
    assert (queued.state, queued.attempt, queued.revision) == ("pending", 0, 1)

    async with space._tx(own=True) as tx:
        (claim,) = await tx.claim_provider_operations("worker-a", limit=5, claim_for=CLAIM)
        assert await tx.claim_provider_operations("worker-b", limit=5, claim_for=CLAIM) == []  # уже занята
    assert (claim.operation_id, claim.attempt) == (queued.id, 1)

    # между транзакциями — вызов адаптера; claim_id — его ключ идемпотентности
    async with space._tx(own=True) as tx:
        assert await tx.apply_provider_operation(claim, "succeeded", result={"outcome": "ok"})
        assert not await tx.apply_provider_operation(claim, "failed", error_code="Late")  # повтор — поздний
        (row,) = await tx.list_provider_operations(identity_id=identity_id, states=None, limit=10)
    assert (row.state, row.result, row.revision) == ("succeeded", {"outcome": "ok"}, 3)
    assert row.finished_at is not None


async def test_expired_claim_is_recovered_and_late_apply_rejected(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(space)
    policy = RetryPolicy(max_attempts=2, base=timedelta(0), max_delay=timedelta(0))
    async with space._tx(own=True) as tx:
        await tx.enqueue_provider_operation(identity_id, "login")
        (first,) = await tx.claim_provider_operations("worker-a", limit=1, claim_for=SHORT)
    await _pass(hub_under_test, 0.6)  # воркер упал между claim и apply

    async with space._tx(own=True) as tx:
        assert await tx.recover_provider_operations(limit=10, policy=policy) == 1
        (second,) = await tx.claim_provider_operations("worker-b", limit=1, claim_for=CLAIM)
        assert not await tx.apply_provider_operation(first, "succeeded")  # ответ старого воркера
        assert second.attempt == 2 and second.claim_token > first.claim_token
        assert await tx.apply_provider_operation(second, "retryable", error_code="Timeout", policy=policy)
        (row,) = await tx.list_provider_operations(identity_id=identity_id, states=None, limit=10)
    assert (row.state, row.error_code) == ("failed", "Timeout")  # попытки исчерпаны


async def test_cancel_only_before_running(space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    async with space._tx(own=True) as tx:
        waiting = await tx.enqueue_provider_operation(identity_id, "probe:auth")
        busy = await tx.enqueue_provider_operation(identity_id, "login")
        assert await tx.cancel_provider_operation(waiting.id)
        assert not await tx.cancel_provider_operation(waiting.id)
        (claim,) = await tx.claim_provider_operations("w", limit=5, claim_for=CLAIM)
        assert claim.operation_id == busy.id
        assert not await tx.cancel_provider_operation(busy.id)  # уже выполняется
