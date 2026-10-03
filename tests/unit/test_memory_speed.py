"""F5.2b (R2-P22): транзакция MemoryStore не копирует всё состояние; откат по-прежнему полный."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from identity_hub import IdentityCreate, IdentityHub, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.crypto.envelope import EncryptedPayload
from identity_hub.events import EventDraft
from identity_hub.testing import MemoryStore, make_service

pytestmark = pytest.mark.unit

TARGET = 0.1  # секунд на цикл lease + operation при 1000 аккаунтах
MARGIN = 3  # запас на загруженную машину


async def _hub(count: int) -> tuple[IdentityHub, object]:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[make_service("svc")],
        store=MemoryStore(clock),
        clock=clock,
    )
    await hub.start()
    space = hub.space("s", "S", create=True)
    specs = [
        IdentityCreate(service="svc", external_ref=f"ref-{n}", lifecycle="ready", session={})
        for n in range(count)
    ]
    for start in range(0, count, 500):
        await space.identities.create_many(specs[start : start + 500], actor="test")
    return hub, space


async def test_lease_and_operation_with_1000_accounts_is_fast() -> None:
    _, space = await _hub(1000)
    pool = space.pools.define("p", service="svc")  # type: ignore[attr-defined]
    async with pool.lease() as lease, lease.operation("work"):  # прогрев
        pass

    started = time.perf_counter()
    rounds = 5
    for _ in range(rounds):
        async with pool.lease() as lease, lease.operation("work"):
            pass
    elapsed = (time.perf_counter() - started) / rounds

    assert elapsed < TARGET * MARGIN, f"цикл аренды {elapsed * 1000:.0f} мс"


async def test_failed_transaction_rolls_back_every_section() -> None:
    """Откат после исключения восстанавливает аккаунты, связи, секреты, события и счётчики."""
    hub, space = await _hub(3)
    store: MemoryStore = hub._store  # type: ignore[assignment]
    first, second, *_ = list(store._state.identities)
    before = {
        "identities": dict(store._state.identities),
        "links": dict(store._state.links),
        "credentials": dict(store._state.credentials),
        "events": list(store._state.events),
        "next_event_id": store._state.next_event_id,
    }

    with pytest.raises(RuntimeError):
        async with space._tx(own=True) as tx:  # type: ignore[attr-defined]
            assert await tx.insert_link(first, "recovery", second)
            await tx.put_credentials(first, EncryptedPayload("k", b"", b"", b""))
            await space._emit(  # type: ignore[attr-defined]
                tx, EventDraft("identity.updated", actor="t", identity_id=first)
            )
            raise RuntimeError("сбой посреди транзакции")

    assert store._state.identities == before["identities"]  # event_seq аккаунта тоже откатился
    assert store._state.links == before["links"]
    assert store._state.credentials == before["credentials"]
    assert store._state.events == before["events"]
    assert store._state.next_event_id == before["next_event_id"]
