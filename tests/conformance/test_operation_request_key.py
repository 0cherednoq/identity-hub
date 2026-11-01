"""AR3.3: `request_key` у операций провайдера — идемпотентность одной логической проверки (D-335)."""

from __future__ import annotations

from uuid import UUID

import pytest

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle
from identity_hub.errors import AlreadyExists

pytestmark = [pytest.mark.asyncio, pytest.mark.conformance]


async def _identity(space: SpaceHandle, ref: str = "+1") -> UUID:
    created = await space.identities.create(IdentityCreate(service="fake_tg", external_ref=ref), actor="test")
    return created.id


async def _run(space: SpaceHandle, identity_id: UUID, key: str | None, outcome: str) -> UUID:
    """Поставить, взять и завершить операцию; вернуть её id."""
    async with space._tx(own=True) as tx:
        queued = await tx.enqueue_provider_operation(identity_id, "login", request_key=key)
        (claim,) = await tx.claim_provider_operations("w", limit=1, claim_for=_CLAIM)
        assert await tx.apply_provider_operation(claim, outcome, result={"outcome": outcome})
    return queued.id


from datetime import timedelta  # noqa: E402

_CLAIM = timedelta(seconds=30)


async def test_request_key_is_stored_and_found(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _identity(space)
    async with space._tx(own=True) as tx:
        queued = await tx.enqueue_provider_operation(identity_id, "login", request_key="r-1:credential")
        found = await tx.get_request_operation(identity_id, "login", "r-1:credential")
        assert await tx.get_request_operation(identity_id, "login", "other") is None
        plain = await tx.enqueue_provider_operation(identity_id, "probe:auth")
    assert queued.request_key == "r-1:credential" and plain.request_key is None
    assert found is not None and found.id == queued.id


async def test_live_and_succeeded_keyed_operation_blocks_repeat(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(space)
    async with space._tx(own=True) as tx:
        await tx.enqueue_provider_operation(identity_id, "login", request_key="r-1")
    with pytest.raises(AlreadyExists):
        async with space._tx(own=True) as tx:
            await tx.enqueue_provider_operation(identity_id, "login", request_key="r-1")

    async with space._tx(own=True) as tx:
        (claim,) = await tx.claim_provider_operations("w", limit=1, claim_for=_CLAIM)
        assert await tx.apply_provider_operation(claim, "succeeded", result={"outcome": "ok"})
    with pytest.raises(AlreadyExists):  # завершённая успешно — повтор получает сохранённый результат
        async with space._tx(own=True) as tx:
            await tx.enqueue_provider_operation(identity_id, "login", request_key="r-1")
    async with space._tx(own=True) as tx:
        row = await tx.get_request_operation(identity_id, "login", "r-1")
    assert row is not None and row.state == "succeeded" and row.result == {"outcome": "ok"}


async def test_failed_keyed_operation_allows_retry_with_same_key(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(space)
    failed_id = await _run(space, identity_id, "r-1", "failed")

    async with space._tx(own=True) as tx:
        assert await tx.get_request_operation(identity_id, "login", "r-1") is None  # failed не блокирует
    second_id = await _run(space, identity_id, "r-1", "succeeded")

    assert failed_id != second_id
    async with space._tx(own=True) as tx:
        row = await tx.get_request_operation(identity_id, "login", "r-1")
    assert row is not None and row.id == second_id


async def test_new_key_after_completion_is_a_new_attempt(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(space)
    first = await _run(space, identity_id, "r-1", "succeeded")
    second = await _run(space, identity_id, "r-2", "succeeded")
    unkeyed = await _run(space, identity_id, None, "succeeded")
    assert len({first, second, unkeyed}) == 3


async def test_keys_are_scoped_per_identity_and_kind(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    one = await _identity(space, "+1")
    two = await _identity(space, "+2")
    await _run(space, one, "r-1", "succeeded")
    await _run(space, two, "r-1", "succeeded")  # другой аккаунт - другая попытка
    async with space._tx(own=True) as tx:
        await tx.enqueue_provider_operation(one, "probe:auth", request_key="r-1")  # другой вид операции
