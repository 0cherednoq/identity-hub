"""IMP-05 / R1-15 / R2-S05 (I2.3): запись от имени аренды — только при полном совпадении fence (D-212)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from identity_hub import IdentityCreate, IdentityHub, LeaseOwnershipLost, SpaceHandle, StaleFencing
from identity_hub.clock import FakeClock
from identity_hub.errors import LeaseLost

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


async def _lease(space: SpaceHandle, **timing: timedelta) -> Any:
    await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}),
        actor="test",
    )
    return await space.pools.define("work", service="fake_tg", holder="w", **timing).acquire()


def _forged(lease: Any, **changes: Any) -> None:
    lease._row = replace(lease._row, **changes)


@pytest.mark.parametrize("field", ["token", "lease_id", "fencing"])
async def test_forged_fence_field_is_rejected(space: SpaceHandle, field: str) -> None:
    lease = await _lease(space)
    original = lease._row
    _forged(lease, **{field: lease.fencing + 1 if field == "fencing" else uuid4()})

    with pytest.raises(LeaseOwnershipLost) as caught:
        await lease.save_session({"auth_key": "forged"})
    assert isinstance(caught.value, LeaseLost) and isinstance(caught.value, StaleFencing)
    with pytest.raises(LeaseOwnershipLost):
        async with lease.operation("read_channel"):
            pass
    with pytest.raises(LeaseOwnershipLost):
        await lease.credentials()  # R2-S05: чтение секрета тоже сверяет fence

    lease._row = original
    await lease.release()


async def test_expired_lease_with_same_fencing_cannot_write(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """R1-15: аренда истекла, аккаунт ещё никто не взял — номер fencing тот же, но запись запрещена."""
    lease = await _lease(
        space,
        ttl=timedelta(seconds=0.6),
        heartbeat=timedelta(seconds=0.2),
        safety_margin=timedelta(seconds=0.3),
        stats_flush=timedelta(seconds=0.2),
    )
    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(timedelta(seconds=2))
    else:
        await asyncio.sleep(0.8)
    # воркер не заметил потерю (часы процесса отстали): отказ должен прийти из БД, а не из lease.check()
    lease._last_ok = hub_under_test.clock.monotonic()
    lease._lost = None

    with pytest.raises(LeaseOwnershipLost):
        async with lease.operation("read_channel"):
            pass
    with pytest.raises(LeaseOwnershipLost):
        await lease.save_session({"auth_key": "zombie"})


async def test_revoked_and_reacquired_lease_is_rejected(space: SpaceHandle) -> None:
    lease = await _lease(space)
    await space.pools.revoke(lease.identity_id, reason="тест", actor="admin")
    lease._lost = None

    with pytest.raises(LeaseOwnershipLost):
        await lease.save_session({"auth_key": "after-revoke"})

    fresh = await space.pools.define("work", service="fake_tg", holder="w2").acquire()
    with pytest.raises(LeaseOwnershipLost):
        await lease.mark_authenticated()
    await fresh.save_session({"auth_key": "fresh"})  # валидный fence пишет данные и событие
    await fresh.release()


def test_fence_hides_token() -> None:
    from identity_hub import LeaseFence

    fence = LeaseFence(identity_id=uuid4(), lease_id=uuid4(), token=uuid4(), fencing=3)
    assert str(fence.token) not in repr(fence)
