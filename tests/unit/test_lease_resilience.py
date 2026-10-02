"""F2.1: heartbeat и освобождение аренды на MemoryStore с задержкой (R1-03, R1-12, R1-14, R1-21)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from identity_hub import IdentityHub, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.errors import LeaseUnavailable
from identity_hub.testing import MemoryStore
from tests.fixtures import lease_resilience as scenarios
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.unit


@pytest.fixture
async def hub() -> IdentityHub:
    """Реальные часы: дедлайн самоограждения считается по `time.monotonic()`."""
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"\x33" * 32}, "k1"), services=[FAKE_TG], store=MemoryStore()
    )
    await hub.start()
    return hub


async def test_single_heartbeat_failure_keeps_lease(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    await scenarios.check_single_heartbeat_failure_keeps_lease(hub, monkeypatch)


async def test_heartbeat_gives_up_at_deadline(hub: IdentityHub, monkeypatch: pytest.MonkeyPatch) -> None:
    await scenarios.check_heartbeat_gives_up_at_deadline(hub, monkeypatch)


async def test_double_cancel_during_release_frees_lease(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    await scenarios.check_double_cancel_during_release_frees_lease(hub, monkeypatch)


async def test_release_validates_reason_before_touching_lease(hub: IdentityHub) -> None:
    await scenarios.check_release_validates_reason_before_touching_lease(hub)


async def test_release_can_be_retried_after_failure(
    hub: IdentityHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    await scenarios.check_release_can_be_retried_after_failure(hub, monkeypatch)


async def test_proxy_pool_exhausted_is_reported_once_per_acquire(hub: IdentityHub) -> None:
    await scenarios.check_proxy_pool_exhausted_is_reported_once_per_acquire(hub)


async def test_proxy_pool_exhausted_repeats_after_a_minute() -> None:
    """R1-12: не чаще одного события в минуту на пул в процессе; через минуту — снова."""
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"\x33" * 32}, "k1"),
        services=[scenarios.PROXIED],
        store=MemoryStore(clock),
        clock=clock,
    )
    await hub.start()
    space = hub.space("exhausted", "Exhausted")
    await register(
        space, service="proxied_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    pool = space.pools.define("px", service="proxied_tg", holder="w")
    other = space.pools.define("px-other", service="proxied_tg", holder="w")

    async def attempt(target: object) -> int:
        with pytest.raises(LeaseUnavailable):
            await target.acquire()  # type: ignore[attr-defined]
        return await scenarios._exhausted_events(space)

    assert await attempt(pool) == 1
    assert await attempt(pool) == 1
    assert await attempt(other) == 2  # другой пул отчитывается сам
    clock.advance(61)
    assert await attempt(pool) == 3
