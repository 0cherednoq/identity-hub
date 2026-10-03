"""F1.2 (IMP-03): ключ идемпотентности аренды — R2-S01, R1-13.

Повтор `pool.acquire(idempotency_key=...)` возвращает ту же аренду только тому же пулу, сервису
и holder. Любой другой заявитель получает `IdempotencyConflict`: ни аренда, ни её `token`, ни
credentials чужого аккаунта ему не выдаются. Ключ действует в пределах пространства.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import IdempotencyConflict, IdentityHub, SpaceHandle

pytestmark = pytest.mark.conformance


async def _tg(space: SpaceHandle, ref: str = "+1") -> None:
    await register(
        space,
        service="fake_tg",
        external_ref=ref,
        lifecycle="ready",
        credentials={"phone": ref, "password": "TG-SECRET"},
        session={"auth_key": "k"},
    )


async def _exchange(space: SpaceHandle, ref: str = "trader@example.com") -> None:
    await register(
        space,
        service="fake_exchange",
        external_ref=ref,
        lifecycle="ready",
        credentials={"email": ref, "password": "EX-SECRET"},
        session={"cookies": {"sid": "1"}},
    )


async def _live(space: SpaceHandle, key: str) -> bool:
    async with space._tx(own=True) as tx:
        return await tx.find_live_lease(key) is not None


async def test_repeat_from_same_pool_and_holder_returns_same_lease(space: SpaceHandle) -> None:
    await _tg(space, "+1")
    await _tg(space, "+2")
    pool = space.pools.define("senders", service="fake_tg", holder="worker-1")
    first = await pool.acquire(idempotency_key="job-42")
    again = await space.pools.define("senders", service="fake_tg", holder="worker-1").acquire(
        idempotency_key="job-42"
    )
    assert (again.lease_id, again.identity_id, again.fencing) == (
        first.lease_id,
        first.identity_id,
        first.fencing,
    )
    other = await pool.acquire(idempotency_key="job-43")
    assert other.identity_id != first.identity_id


async def test_pool_of_another_service_gets_conflict_not_foreign_account(space: SpaceHandle) -> None:
    """R2-S01: пул другого сервиса не получает чужую аренду, token и credentials."""
    await _tg(space)
    await _exchange(space)
    senders = space.pools.define("senders", service="fake_tg", holder="worker-1")
    traders = space.pools.define("traders", service="fake_exchange", holder="worker-2")
    mine = await senders.acquire(idempotency_key="job-42")

    with pytest.raises(IdempotencyConflict) as exc:
        await traders.acquire(idempotency_key="job-42")

    assert "job-42" not in str(exc.value) and "job-42" not in str(exc.value.context)
    assert await _live(space, "job-42")  # аренда владельца не тронута
    assert (await mine.credentials()).password.get_secret_value() == "TG-SECRET"
    own = await traders.acquire(idempotency_key="job-other")  # своим ключом пул работает
    assert own.identity_id != mine.identity_id


@pytest.mark.parametrize(
    ("name", "holder"),
    [("other-pool", "worker-1"), ("senders", "worker-2")],
    ids=["another-pool", "another-holder"],
)
async def test_another_pool_or_holder_gets_conflict(space: SpaceHandle, name: str, holder: str) -> None:
    await _tg(space, "+1")
    await _tg(space, "+2")
    await space.pools.define("senders", service="fake_tg", holder="worker-1").acquire(
        idempotency_key="job-42"
    )
    with pytest.raises(IdempotencyConflict):
        await space.pools.define(name, service="fake_tg", holder=holder).acquire(idempotency_key="job-42")


async def test_same_key_in_two_spaces_gives_independent_leases(hub_under_test: IdentityHub) -> None:
    """R1-13: тот же ключ в другом пространстве — своя аренда, а не ошибка уникального индекса."""
    leases = []
    for _ in range(2):
        space = hub_under_test.space(f"s-{uuid4().hex[:8]}", create=True)
        await _tg(space, f"+{uuid4().int % 10**9}")
        pool = space.pools.define("senders", service="fake_tg", holder="worker-1")
        leases.append(await pool.acquire(idempotency_key="job-42"))
    assert leases[0].lease_id != leases[1].lease_id
    assert leases[0].identity_id != leases[1].identity_id


async def test_key_is_reusable_by_its_owner_after_release(space: SpaceHandle) -> None:
    await _tg(space)
    pool = space.pools.define("senders", service="fake_tg", holder="worker-1")
    first = await pool.acquire(idempotency_key="job-42")
    await first.release()
    again = await pool.acquire(idempotency_key="job-42")
    assert again.lease_id != first.lease_id
    assert again.fencing == first.fencing + 1


async def test_released_key_still_conflicts_for_another_command(space: SpaceHandle) -> None:
    await _tg(space)
    await _exchange(space)
    first = await space.pools.define("senders", service="fake_tg", holder="w").acquire(
        idempotency_key="job-42"
    )
    await first.release()
    with pytest.raises(IdempotencyConflict):
        await space.pools.define("traders", service="fake_exchange", holder="w").acquire(
            idempotency_key="job-42"
        )


async def test_live_lease_is_checked_even_when_key_record_expired(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """Запись ключа истекла раньше аренды: чужой пул всё равно не получает живую аренду."""
    hub_under_test.retention = replace(hub_under_test.retention, operation_keys=timedelta(0))
    await _tg(space)
    await _exchange(space)
    senders = space.pools.define("senders", service="fake_tg", holder="w")
    first = await senders.acquire(idempotency_key="job-42")
    with pytest.raises(IdempotencyConflict):
        await space.pools.define("traders", service="fake_exchange", holder="w").acquire(
            idempotency_key="job-42"
        )
    assert (await senders.acquire(idempotency_key="job-42")).lease_id == first.lease_id
