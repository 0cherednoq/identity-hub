"""N0.1 (задачи 1, 4, 5): продление сообщает, чем кончилась аренда, а отказ — почему отказано."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from tests.fixtures.lease_resilience import patch_heartbeat
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, LeaseExpired, LeaseMode, LeaseRevoked, SpaceHandle
from identity_hub.clock import FakeClock
from identity_hub.enums import LeaseEndReason, LeaseUnavailableReason
from identity_hub.errors import LeaseUnavailable
from identity_hub.lease import Lease
from identity_hub.store.base import LeaseCounters, LeaseRenewal

pytestmark = [pytest.mark.conformance]

SHORT = {
    "ttl": timedelta(seconds=0.6),
    "heartbeat": timedelta(seconds=0.2),
    "safety_margin": timedelta(seconds=0.3),
}


async def _ready(space: SpaceHandle, count: int = 1) -> None:
    await space.identities.create_many(
        [
            IdentityCreate(
                service="fake_tg", external_ref=f"+{n}", lifecycle="ready", session={"auth_key": "k"}
            )
            for n in range(count)
        ],
        actor="test",
    )


async def _expire(hub: IdentityHub) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=2))
    else:
        await asyncio.sleep(0.8)


async def _both_paths(
    hub: IdentityHub, space: SpaceHandle, lease: Lease
) -> tuple[LeaseRenewal, LeaseRenewal]:
    """Ответ пакетного и одиночного продления одной аренды."""
    batch = (await hub._store.renew_leases([lease._row.token], 60.0))[lease._row.token]
    async with space._tx(own=True) as tx:
        single = await tx.extend_lease(lease._row, 60.0, LeaseCounters.empty(), shared=False)
    return batch, single


async def test_live_lease_renewal_has_no_end_reason(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()

    for renewal in await _both_paths(hub_under_test, space, lease):
        assert renewal.expires_at is not None and renewal.expires_at >= lease.expires_at
        assert renewal.end_reason is None

    await lease.release()


async def test_revoked_lease_reports_revoked(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()
    assert await space.pools.revoke(lease.identity_id, reason="тест", actor="admin")

    for renewal in await _both_paths(hub_under_test, space, lease):
        assert renewal == LeaseRenewal(None, LeaseEndReason.REVOKED)


async def test_expired_lease_reports_expired(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **SHORT)
    lease = await pool.acquire()
    await _expire(hub_under_test)

    for renewal in await _both_paths(hub_under_test, space, lease):  # срок вышел, строку ещё не забрали
        assert renewal == LeaseRenewal(None, LeaseEndReason.EXPIRED)

    successor = await pool.acquire()  # перезахват закрывает историю как expired
    for renewal in await _both_paths(hub_under_test, space, lease):
        assert renewal == LeaseRenewal(None, LeaseEndReason.EXPIRED)
    await successor.release()


async def test_released_lease_reports_released(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _ready(space)
    lease = await space.pools.define("work", service="fake_tg", holder="w").acquire()
    await lease.release()

    for renewal in await _both_paths(hub_under_test, space, lease):
        assert renewal == LeaseRenewal(None, LeaseEndReason.RELEASED)


async def test_unknown_token_has_no_reason(hub_under_test: IdentityHub) -> None:
    token = uuid4()

    assert await hub_under_test._store.renew_leases([token], 60.0) == {token: LeaseRenewal(None, None)}


async def test_shared_lease_reports_end_reason(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    hub_under_test.register_service(replace(FAKE_TG, name="fake_shared", lease_mode=LeaseMode.SHARED))
    await hub_under_test.start()  # строка сервиса в БД
    await space.identities.create(
        IdentityCreate(
            service="fake_shared", external_ref="acc", lifecycle="ready", session={"auth_key": "k"}
        ),
        actor="test",
    )
    lease = await space.pools.define("work", service="fake_shared", holder="w").acquire()

    async with space._tx(own=True) as tx:
        live = await tx.extend_lease(lease._row, 60.0, LeaseCounters.empty(), shared=True)
    assert live.expires_at is not None and live.end_reason is None

    await lease.release()
    async with space._tx(own=True) as tx:
        gone = await tx.extend_lease(lease._row, 60.0, LeaseCounters.empty(), shared=True)
    assert gone == LeaseRenewal(None, LeaseEndReason.RELEASED)


@pytest.mark.parametrize(
    ("end_reason", "error", "text"),
    [
        (LeaseEndReason.EXPIRED, LeaseExpired, "expired"),
        (LeaseEndReason.REVOKED, LeaseRevoked, "revoked"),
        (None, LeaseRevoked, "revoked"),  # строки с нашим token нет, история открыта: аренду сняли
    ],
)
async def test_holder_gets_the_reason_from_renewal(
    hub_under_test: IdentityHub,
    space: SpaceHandle,
    monkeypatch: pytest.MonkeyPatch,
    end_reason: LeaseEndReason | None,
    error: type[Exception],
    text: str,
) -> None:
    await _ready(space)
    pool = space.pools.define("work", service="fake_tg", holder="w", **SHORT)

    async def gone(self: Any, tokens_or_lease: Any, *args: Any, **kwargs: Any) -> Any:
        renewal = LeaseRenewal(None, end_reason)
        return renewal if hasattr(tokens_or_lease, "token") else dict.fromkeys(tokens_or_lease, renewal)

    with pytest.raises(error) as caught:
        async with pool.lease():
            patch_heartbeat(hub_under_test, monkeypatch, gone)
            for _ in range(200):
                if isinstance(hub_under_test.clock, FakeClock):
                    hub_under_test.clock.advance(timedelta(seconds=0.1))
                await asyncio.sleep(0.01)
    monkeypatch.undo()

    assert type(caught.value) is error
    assert caught.value.reason == text  # type: ignore[attr-defined]


async def test_busy_and_no_candidates_are_told_apart(space: SpaceHandle) -> None:
    pool = space.pools.define("work", service="fake_tg", holder="w")

    with pytest.raises(LeaseUnavailable) as empty:
        await pool.acquire()
    assert empty.value.reason is LeaseUnavailableReason.NO_CANDIDATES
    assert empty.value.context == {"retry_after": None, "reason": "no_candidates"}

    await _ready(space)
    lease = await pool.acquire()
    with pytest.raises(LeaseUnavailable) as busy:
        await pool.acquire()
    assert busy.value.reason is LeaseUnavailableReason.BUSY
    assert busy.value.context["reason"] == "busy"

    await lease.release()
