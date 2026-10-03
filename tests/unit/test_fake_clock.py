"""F2.3: часы и ожидание под `FakeClock` (R2-P01, R2-P05, R2-P21)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from identity_hub import IdentityHub, LeaseLost, StaticSecretsProvider, ValidationFailed
from identity_hub.clock import FakeClock, SystemClock
from identity_hub.dispatch import make_signature, verify_signature
from identity_hub.errors import LeaseUnavailable
from identity_hub.testing import MemoryStore
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.unit

SECRETS = StaticSecretsProvider({"k1": b"\x33" * 32}, "k1")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
async def hub(clock: FakeClock) -> IdentityHub:
    hub = IdentityHub(secrets=SECRETS, services=[FAKE_TG], store=MemoryStore(), clock=clock)
    await hub.start()
    return hub


async def _account(hub: IdentityHub, ref: str = "+1") -> object:
    space = hub.space("clock", "Clock", create=True)
    await register(space, service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"})
    return space


# ---------------------------------------------------------------- сами часы


async def test_sleep_moves_fake_time_and_yields(clock: FakeClock) -> None:
    started = clock.monotonic()
    before = clock.now()

    await asyncio.wait_for(clock.sleep(30), timeout=1)  # реального ожидания нет

    assert clock.monotonic() - started == 30
    assert clock.now() - before == timedelta(seconds=30)


async def test_idle_waits_until_someone_advances_time(clock: FakeClock) -> None:
    woke: list[float] = []

    async def background(seconds: float) -> None:
        await clock.idle(seconds)
        woke.append(clock.monotonic())

    tasks = [asyncio.create_task(background(s)) for s in (20, 5)]
    await asyncio.sleep(0.01)
    assert woke == []  # фоновое ожидание само время не двигает

    clock.advance(10)
    await asyncio.sleep(0.01)
    assert woke == [10]

    await clock.sleep(60)  # проходит через каждое пробуждение по порядку
    assert woke == [10, 20]
    await asyncio.gather(*tasks)


async def test_system_clock_sleeps_for_real() -> None:
    clock = SystemClock()
    started = clock.monotonic()
    await clock.sleep(0.05)
    await clock.idle(0.05)
    assert clock.monotonic() - started >= 0.09


# ---------------------------------------------------------------- аренда под FakeClock


async def test_wait_expires_under_fake_clock(hub: IdentityHub, clock: FakeClock) -> None:
    """R2-P01: `wait=` при пустом пуле заканчивается `LeaseUnavailable`, а не зависает."""
    space = hub.space("clock", "Clock", create=True)
    pool = space.pools.define("p", service="fake_tg")
    started = clock.monotonic()

    with pytest.raises(LeaseUnavailable):
        await asyncio.wait_for(pool.acquire(wait=30), timeout=5)

    assert 30 <= clock.monotonic() - started < 40


async def test_advance_inside_lease_does_not_lose_it(hub: IdentityHub, clock: FakeClock) -> None:
    """R2-P05: сдвиг часов будит heartbeat — аренда продлевается, как у живого воркера."""
    space = await _account(hub)
    pool = space.pools.define("p", service="fake_tg")  # type: ignore[attr-defined]

    async with pool.lease() as lease:
        first_expiry = lease.expires_at
        clock.advance(45)  # дольше ttl − safety_margin (40 с)
        await asyncio.sleep(0.05)
        lease.check()
        assert not lease.lost
        assert lease.expires_at > first_expiry

        await clock.sleep(600)  # долгий скачок проходит через каждый heartbeat
        lease.check()
        assert await lease.session() is not None


async def test_revoke_plus_advance_fires_on_lost(hub: IdentityHub, clock: FakeClock) -> None:
    space = await _account(hub)
    pool = space.pools.define("p", service="fake_tg")  # type: ignore[attr-defined]
    lost: list[str] = []

    with pytest.raises(LeaseLost) as exc:
        async with pool.lease(on_lost=lambda: lost.append("called")) as lease:
            assert await space.pools.revoke(lease.identity_id, reason="тест", actor="admin")  # type: ignore[attr-defined]
            clock.advance(21)  # до следующего heartbeat
            await asyncio.sleep(5)

    assert exc.value.reason == "revoked"
    assert lost == ["called"]


# ---------------------------------------------------------------- согласованность часов


async def test_memory_store_adopts_hub_clock(clock: FakeClock) -> None:
    """R2-P21: `MemoryStore()` без часов работает по часам хаба, а не по системным."""
    store = MemoryStore()
    hub = IdentityHub(secrets=SECRETS, services=[FAKE_TG], store=store, clock=clock)
    await hub.start()
    assert store.clock is clock and hub.clock is clock

    space = hub.space("clock", "Clock", create=True)
    identity = await register(space, service="fake_tg", external_ref="+1")
    created = (await space.identities.get(identity.identity_id)).created_at
    assert created == clock.now()


def test_hub_adopts_clock_of_memory_store(clock: FakeClock) -> None:
    hub = IdentityHub(secrets=SECRETS, store=MemoryStore(clock))
    assert hub.clock is clock


def test_two_different_clocks_are_a_configuration_error(clock: FakeClock) -> None:
    with pytest.raises(ValidationFailed):
        IdentityHub(secrets=SECRETS, store=MemoryStore(FakeClock()), clock=clock)
    shared = MemoryStore(clock)
    IdentityHub(secrets=SECRETS, store=shared, clock=clock)  # одни и те же часы — нормально
    IdentityHub(secrets=SECRETS, store=shared)


def test_verify_signature_uses_given_clock(clock: FakeClock) -> None:
    """R2-P21: подпись, сделанная под `FakeClock`, проверяется с `now=` тех же часов."""
    body = b'{"type":"identity.registered"}'
    header = make_signature(body, "whsec", timestamp=int(clock.now().timestamp()))

    assert verify_signature(body, header, "whsec", now=clock.now())
    assert not verify_signature(body, header, "whsec")  # системное время далеко от 2026-01-01
    clock.advance(timedelta(minutes=6))
    assert not verify_signature(body, header, "whsec", now=clock.now())  # окно повтора — 5 минут
