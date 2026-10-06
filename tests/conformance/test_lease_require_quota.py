"""N1.3 (задача 5): `require_quota` отсекает аккаунты с исчерпанной квотой на этапе выборки."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdentityCreate,
    IdentityHub,
    LeaseMode,
    LeaseUnavailable,
    LeaseUnavailableReason,
    QuotaExhausted,
    SpaceHandle,
    ValidationFailed,
)
from identity_hub.service import OperationRule, Quota

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

WINDOW = timedelta(hours=1)
# две квоты: короткое окно исчерпывается первым, длинное — нет
QUOTA_TG = replace(
    FAKE_TG,
    name="quota_tg",
    operations={
        "send": OperationRule(quotas=(Quota(2, WINDOW), Quota(50, timedelta(days=1)))),
        "read": OperationRule(),
    },
)


async def _space(hub: IdentityHub, space: SpaceHandle, count: int, service: Any = QUOTA_TG) -> list[UUID]:
    hub.register_service(service)
    await hub.start()  # строка сервиса в БД
    created = await space.identities.create_many(
        [
            IdentityCreate(
                service=service.name, external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(count)
        ],
        actor="test",
    )
    return [item.id for item in created.created]


async def _exhaust(pool: Any, identity_id: UUID) -> None:
    lease = await pool.acquire(among=[identity_id])
    for _ in range(2):
        async with lease.operation("send"):
            pass
    with pytest.raises(QuotaExhausted):
        async with lease.operation("send"):
            pass
    await lease.release()


async def test_exhausted_accounts_are_skipped(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    first, second, third = await _space(hub_under_test, space, 3)
    pool = space.pools.define("work", service="quota_tg", holder="w")
    await _exhaust(pool, first)
    await _exhaust(pool, second)

    for _ in range(3):  # LRU отдал бы исчерпанные: с фильтром каждый раз выдаётся свободный
        lease = await pool.acquire(for_operation="send", require_quota=True)
        assert lease.identity_id == third
        await lease.release()


async def test_default_behaviour_is_unchanged(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    (only,) = await _space(hub_under_test, space, 1)
    pool = space.pools.define("work", service="quota_tg", holder="w")
    await _exhaust(pool, only)

    lease = await pool.acquire(for_operation="send")  # без флага исчерпанный аккаунт выдаётся
    assert lease.identity_id == only
    await lease.release()


async def test_all_exhausted_reports_quota_and_retry_after(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    ids = await _space(hub_under_test, space, 2)
    pool = space.pools.define("work", service="quota_tg", holder="w")
    for identity_id in ids:
        await _exhaust(pool, identity_id)

    with pytest.raises(LeaseUnavailable) as refused:
        await pool.acquire(for_operation="send", require_quota=True)

    assert refused.value.reason is LeaseUnavailableReason.QUOTA
    now = hub_under_test.clock.now() if hasattr(hub_under_test.clock, "advance") else datetime.now(UTC)
    window_end = Quota(2, WINDOW).window_start(now) + WINDOW
    assert refused.value.retry_after == pytest.approx((window_end - now).total_seconds(), abs=5)
    assert 0 < refused.value.retry_after <= WINDOW.total_seconds()


async def test_busy_is_not_mistaken_for_quota(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _space(hub_under_test, space, 1)
    pool = space.pools.define("work", service="quota_tg", holder="w")
    held = await pool.acquire()

    with pytest.raises(LeaseUnavailable) as refused:
        await pool.acquire(for_operation="send", require_quota=True)

    assert refused.value.reason is LeaseUnavailableReason.BUSY and refused.value.retry_after is None
    await held.release()


async def test_other_operation_is_not_affected(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    (only,) = await _space(hub_under_test, space, 1)
    pool = space.pools.define("work", service="quota_tg", holder="w")
    await _exhaust(pool, only)

    lease = await pool.acquire(for_operation="read", require_quota=True)  # у read квот нет
    assert lease.identity_id == only
    await lease.release()


async def test_require_quota_needs_an_operation(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _space(hub_under_test, space, 1)
    pool = space.pools.define("work", service="quota_tg", holder="w")

    with pytest.raises(ValidationFailed):
        await pool.acquire(require_quota=True)

    lease = await pool.acquire()  # аккаунт не занят неудачной попыткой
    await lease.release()


async def test_lease_scope_accepts_the_flag(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    exhausted, free = await _space(hub_under_test, space, 2)
    pool = space.pools.define("work", service="quota_tg", holder="w")
    await _exhaust(pool, exhausted)

    async with pool.lease(for_operation="send", require_quota=True) as lease:
        assert lease.identity_id == free
        async with lease.operation("send"):
            pass


async def test_shared_pool_filters_too(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    shared = replace(QUOTA_TG, name="quota_shared", lease_mode=LeaseMode.SHARED)
    exhausted, free = await _space(hub_under_test, space, 2, shared)
    pool = space.pools.define("read", service="quota_shared", holder="r")
    await _exhaust(pool, exhausted)

    for _ in range(3):
        lease = await pool.acquire(for_operation="send", require_quota=True)
        assert lease.identity_id == free
        await lease.release()
