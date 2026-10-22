"""I5.3 (`IMP-21`, `D-328`): необязательный порядок доставок внутри потока одного аккаунта."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle
from identity_hub.events import EventDraft

pytestmark = pytest.mark.conformance

CLAIM = timedelta(seconds=30)


async def _endpoint(space: SpaceHandle, name: str, *, ordered: bool = False) -> UUID:
    endpoint = await space.webhooks.create(
        url=f"https://hooks.example.test/{name}",
        secret=SecretStr("s"),
        event_types=["identity.updated"],
        ordered=ordered,
    )
    return endpoint.id


async def _identity(space: SpaceHandle, ref: str) -> UUID:
    created = await space.identities.create(IdentityCreate(service="fake_tg", external_ref=ref), actor="test")
    return created.id


async def _changes(space: SpaceHandle, identity_id: UUID, count: int) -> None:
    """Несколько изменений аккаунта: каждое поднимает ревизию и даёт событие."""
    for n in range(count):
        await space.identities.set_note(identity_id, f"note-{n}", actor="test")


async def _pump(hub: IdentityHub) -> None:
    await hub._store.process_events("webhooks", hub._webhook_dispatcher.handle_events, limit=100)


async def _claim(hub: IdentityHub, limit: int = 10) -> list[Any]:
    return await hub._store.claim_webhook_deliveries(limit=limit, claim_for=CLAIM)


async def _finish(hub: IdentityHub, attempt: Any, *, succeeded: bool, max_failures: int = 10) -> bool:
    return await hub._store.finish_webhook_delivery(
        attempt.delivery_id,
        attempt.attempt,
        succeeded=succeeded,
        response_code=204 if succeeded else 500,
        response_ms=1,
        error=None if succeeded else "HTTP 500",
        retry_after=timedelta(seconds=0),
        max_failures=max_failures,
    )


async def test_unordered_endpoint_claims_whole_batch(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """По умолчанию порядок не навязывается: прежняя семантика (`D-328`)."""
    await _endpoint(space, "plain")
    identity_id = await _identity(space, "+70000100001")
    await _changes(space, identity_id, 3)
    await _pump(hub_under_test)

    claimed = await _claim(hub_under_test)
    assert len(claimed) == 3


async def test_ordered_endpoint_claims_one_revision_at_a_time(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    endpoint_id = await _endpoint(space, "ordered", ordered=True)
    identity_id = await _identity(space, "+70000100002")
    await _changes(space, identity_id, 3)
    await _pump(hub_under_test)

    first = await _claim(hub_under_test)
    assert len(first) == 1 and first[0].endpoint_id == endpoint_id
    assert await _claim(hub_under_test) == []  # следующая ревизия ждёт

    assert await _finish(hub_under_test, first[0], succeeded=True)
    second = await _claim(hub_under_test)
    assert len(second) == 1 and second[0].delivery_id != first[0].delivery_id

    # порядок — по ревизии аккаунта
    deliveries = await space.webhooks.deliveries(endpoint_id)
    succeeded = [d for d in deliveries if d.status == "succeeded"]
    assert len(succeeded) == 1


async def test_failed_and_dead_delivery_blocks_the_stream_until_it_succeeds(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    await _endpoint(space, "blocking", ordered=True)
    identity_id = await _identity(space, "+70000100003")
    await _changes(space, identity_id, 2)
    await _pump(hub_under_test)

    (first,) = await _claim(hub_under_test)
    assert await _finish(hub_under_test, first, succeeded=False, max_failures=1)  # сразу dead

    # мёртвая доставка блокирует поток: пропуска нет (`D-328` п. 4)
    assert await _claim(hub_under_test) == []

    # оператор повторяет застрявшую доставку, и после успеха поток идёт дальше
    await space.webhooks.retry(first.delivery_id)
    (again,) = await _claim(hub_under_test)
    assert again.delivery_id == first.delivery_id
    assert await _finish(hub_under_test, again, succeeded=True)
    (second,) = await _claim(hub_under_test)
    assert second.delivery_id != first.delivery_id


async def test_other_accounts_and_endpoints_are_not_blocked(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    ordered_id = await _endpoint(space, "ordered-two", ordered=True)
    plain_id = await _endpoint(space, "plain-two")
    first_identity = await _identity(space, "+70000100004")
    second_identity = await _identity(space, "+70000100005")
    await _changes(space, first_identity, 2)
    await _changes(space, second_identity, 2)
    await _pump(hub_under_test)

    claimed = await _claim(hub_under_test)
    by_endpoint: dict[UUID, int] = {}
    for item in claimed:
        by_endpoint[item.endpoint_id] = by_endpoint.get(item.endpoint_id, 0) + 1

    assert by_endpoint[plain_id] == 4  # неупорядоченный endpoint берёт всё
    assert by_endpoint[ordered_id] == 2  # по одной доставке на каждый аккаунт


async def test_events_without_identity_revision_are_never_blocked(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """События без аккаунта и без ревизии не участвуют в порядке (`D-328` п. 2)."""
    await _endpoint(space, "ordered-free", ordered=True)
    for n in range(3):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))
    await _pump(hub_under_test)

    assert len(await _claim(hub_under_test)) == 3


async def test_ordered_flag_is_visible_and_switchable(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    endpoint_id = await _endpoint(space, "switch", ordered=True)
    identity_id = await _identity(space, "+70000100006")
    await _changes(space, identity_id, 2)
    await _pump(hub_under_test)
    assert len(await _claim(hub_under_test)) == 1

    center = await space.admin.delivery_center()
    shown = {item.id: item for item in center.webhook_endpoints}
    assert shown[endpoint_id].ordered is True

    # выключение порядка снимает блокировку немедленно
    await space.webhooks.update(
        endpoint_id,
        url="https://hooks.example.test/switch",
        event_types=["identity.updated"],
        ordered=False,
    )
    assert len(await _claim(hub_under_test)) == 1
    async with space._tx(own=True) as tx:
        assert (await tx.get_webhook_endpoint(endpoint_id)).ordered is False


async def test_blocked_counter_is_visible_to_operator(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Head-of-line блокировка наблюдаема в сводке доставок (`D-328` п. 4)."""
    endpoint_id = await _endpoint(space, "observable", ordered=True)
    identity_id = await _identity(space, "+70000100007")
    await _changes(space, identity_id, 3)
    await _pump(hub_under_test)

    center = await space.admin.delivery_center()
    shown = {item.id: item for item in center.webhook_endpoints}[endpoint_id]
    assert shown.ordered is True and shown.blocked == 2  # ждут две следующие ревизии

    (first,) = await _claim(hub_under_test)
    assert await _finish(hub_under_test, first, succeeded=True)
    center = await space.admin.delivery_center()
    shown = {item.id: item for item in center.webhook_endpoints}[endpoint_id]
    assert shown.blocked == 1
