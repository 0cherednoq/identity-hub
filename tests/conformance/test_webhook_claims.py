"""F3.2: захват, возврат и завершение доставок вебхуков одинаковы на обоих хранилищах (R2-P17)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from identity_hub import IdentityHub, SpaceHandle
from identity_hub.events import EventDraft

pytestmark = pytest.mark.conformance

CLAIM = timedelta(seconds=30)


async def _endpoint(space: SpaceHandle, name: str) -> UUID:
    endpoint = await space.webhooks.create(
        url=f"https://hooks.example.test/{name}", secret=SecretStr("s"), event_types=["identity.updated"]
    )
    return endpoint.id


async def _enqueue(hub: IdentityHub, space: SpaceHandle, count: int) -> None:
    """События записаны и разложены по доставкам; сама отправка не запускается."""
    for n in range(count):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))
    await hub._store.process_events("webhooks", hub._webhook_dispatcher.handle_events, limit=100)


async def _finish(hub: IdentityHub, attempt: Any, *, succeeded: bool, max_failures: int = 10) -> bool:
    return await hub._store.finish_webhook_delivery(
        attempt.delivery_id,
        attempt.attempt,
        succeeded=succeeded,
        response_code=204 if succeeded else 500,
        response_ms=1,
        error=None if succeeded else "HTTP 500",
        retry_after=timedelta(seconds=30),
        max_failures=max_failures,
    )


async def _row(space: SpaceHandle, endpoint_id: UUID) -> Any:
    async with space._tx(own=True) as tx:
        return await tx.get_webhook_endpoint(endpoint_id)


async def test_claim_limit_applies_after_disabled_endpoints_are_filtered(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R2-P17: доставки отключённого endpoint'а не занимают место в пачке и не блокируют живой."""
    paused = await _endpoint(space, "paused")
    await _enqueue(hub_under_test, space, 2)  # две старые доставки — только у paused
    live = await _endpoint(space, "live")
    await _enqueue(hub_under_test, space, 1)
    await space.webhooks.set_enabled(paused, False, reason="maintenance")

    claimed = await hub_under_test._store.claim_webhook_deliveries(limit=1, claim_for=CLAIM)

    assert [(item.endpoint_id, item.attempt) for item in claimed] == [(live, 1)]
    assert await hub_under_test._store.claim_webhook_deliveries(limit=10, claim_for=CLAIM) == []


@pytest.mark.parametrize("succeeded", [True, False], ids=["success", "failure"])
async def test_finish_keeps_manual_disable_reason(
    hub_under_test: IdentityHub, space: SpaceHandle, succeeded: bool
) -> None:
    """R2-P17: результат доставки, начатой до ручного отключения, не стирает его причину."""
    endpoint_id = await _endpoint(space, "manual")
    await _enqueue(hub_under_test, space, 1)
    (claimed,) = await hub_under_test._store.claim_webhook_deliveries(limit=10, claim_for=CLAIM)
    await space.webhooks.set_enabled(endpoint_id, False, reason="maintenance")

    assert await _finish(hub_under_test, claimed, succeeded=succeeded, max_failures=1) is True

    row = await _row(space, endpoint_id)
    assert (row.enabled, row.disabled_reason) == (False, "maintenance")
    (delivery,) = await space.webhooks.deliveries(endpoint_id)
    assert delivery.status == ("succeeded" if succeeded else "dead")


async def test_failure_streak_disables_enabled_endpoint(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    endpoint_id = await _endpoint(space, "flaky")
    await _enqueue(hub_under_test, space, 1)
    (claimed,) = await hub_under_test._store.claim_webhook_deliveries(limit=10, claim_for=CLAIM)

    assert await _finish(hub_under_test, claimed, succeeded=False, max_failures=1) is True
    assert await _finish(hub_under_test, claimed, succeeded=False, max_failures=1) is False  # повтор отклонён

    row = await _row(space, endpoint_id)
    assert (row.enabled, row.disabled_reason, row.failure_streak) == (False, "consecutive_failures", 1)


async def test_released_deliveries_return_without_counting_the_attempt(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Захваченные, но не отправленные доставки возвращаются в очередь с прежним номером попытки."""
    endpoint_id = await _endpoint(space, "release")
    await _enqueue(hub_under_test, space, 3)
    store = hub_under_test._store
    first, second, third = await store.claim_webhook_deliveries(limit=10, claim_for=CLAIM)
    assert await _finish(hub_under_test, first, succeeded=True)

    released = await store.release_webhook_deliveries(
        [(first.delivery_id, first.attempt), (second.delivery_id, second.attempt), (third.delivery_id, 7)],
        retry_after=timedelta(0),
    )

    assert released == 1  # завершённая и доставка с чужим номером попытки не тронуты
    by_id = {row.id: row for row in await space.webhooks.deliveries(endpoint_id)}
    assert (by_id[first.delivery_id].status, by_id[first.delivery_id].attempt) == ("succeeded", 1)
    assert (by_id[second.delivery_id].status, by_id[second.delivery_id].attempt) == ("pending", 0)
    assert by_id[third.delivery_id].attempt == 1
    again = await store.claim_webhook_deliveries(limit=10, claim_for=CLAIM)
    assert [(item.delivery_id, item.attempt) for item in again] == [(second.delivery_id, 1)]

    later = await store.release_webhook_deliveries(
        [(second.delivery_id, 1)], retry_after=timedelta(minutes=5)
    )
    assert later == 1
    assert await store.claim_webhook_deliveries(limit=10, claim_for=CLAIM) == []  # отложена на 5 минут
