"""Общий поведенческий контракт Store для PostgreSQL и MemoryStore."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.clock import FakeClock
from identity_hub.domain.regions import RegionState
from identity_hub.events import EventDraft
from identity_hub.postgres.store import PgStore
from identity_hub.store.base import (
    CandidateQuery,
    LeaseCounters,
    NewIdentity,
    QuotaCharge,
    ServiceRow,
    Store,
)
from identity_hub.testing import MemoryStore

pytestmark = pytest.mark.anyio


@pytest.fixture(params=["memory", "postgres"], ids=["memory", "postgres"])
async def store(
    request: pytest.FixtureRequest, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[Store]:
    if request.param == "memory":
        yield MemoryStore(FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
        return
    yield PgStore(sessionmaker)


@pytest.fixture
async def store_space(store: Store) -> Any:
    await store.sync_services([ServiceRow("svc", "Service")])
    return await store.ensure_space("conformance", "Conformance")


def identity(external_ref: str = "account-1", *, identity_id: UUID | None = None) -> NewIdentity:
    return NewIdentity(
        id=identity_id or uuid4(),
        service="svc",
        external_ref=external_ref,
        lifecycle="ready",
        standing="good",
        auth_state="authenticated",
        tags=["worker"],
        attributes={"shard": 1},
        profile={"locale": "ru"},
        labels={"team": "qa"},
        note="контракт",
        search_text=f"{external_ref} worker qa",
    )


@asynccontextmanager
async def tx(store: Store, space: Any) -> AsyncIterator[Any]:
    async with store.transaction(space) as transaction:
        yield transaction


async def test_transaction_rolls_back(store: Store, store_space: Any) -> None:
    row = identity()
    with pytest.raises(RuntimeError, match="rollback"):
        async with tx(store, store_space) as transaction:
            assert await transaction.insert_identity(row)
            raise RuntimeError("rollback")

    async with tx(store, store_space) as transaction:
        assert await transaction.get_identity(row.id) is None


async def test_identity_region_and_data_compare_and_set(store: Store, store_space: Any) -> None:
    row = identity()
    async with tx(store, store_space) as transaction:
        assert await transaction.insert_identity(row)
        assert not await transaction.insert_identity(row)
        current = await transaction.get_identity(row.id)
        assert current is not None
        assert await transaction.update_identity(row.id, {"note": "new"}, {"note": "контракт"})
        assert not await transaction.update_identity(row.id, {"note": "lost"}, {"note": "контракт"})

        now = await transaction.now()
        first = RegionState("good", now)
        second = RegionState("limited", now, version=2, code="rate_limit")
        assert await transaction.save_region(row.id, "standing", "", None, first)
        assert not await transaction.save_region(row.id, "standing", "", None, first)
        assert await transaction.save_region(row.id, "standing", "", first, second)
        assert not await transaction.save_region(row.id, "standing", "", first, second)

        assert await transaction.save_data(row.id, "app", {"value": 1}, 1, None) == 1
        assert await transaction.save_data(row.id, "app", {"value": 2}, 1, None) is None
        assert await transaction.save_data(row.id, "app", {"value": 2}, 1, 1) == 2


async def test_lease_fencing_and_idempotency(store: Store, store_space: Any) -> None:
    row = identity()
    async with tx(store, store_space) as transaction:
        assert await transaction.insert_identity(row)
        candidate = await transaction.pick_candidate(
            CandidateQuery("svc", frozenset({"good"}), tags=frozenset({"worker"}))
        )
        assert candidate is not None
        lease, reclaimed = await transaction.acquire_lease(
            candidate,
            pool="default",
            holder="worker-1",
            ttl_seconds=30,
            idempotency_key="lease-1",
            shared=False,
        )
        assert reclaimed is None
        assert lease.fencing == 1
        assert await transaction.find_live_lease("lease-1") == lease
        assert await transaction.current_fencing(row.id) == 1
        assert await transaction.release_lease(lease, "released", LeaseCounters.empty(), shared=False)
        assert not await transaction.release_lease(lease, "released", LeaseCounters.empty(), shared=False)


async def test_quota_charge_is_atomic_and_idempotent(store: Store, store_space: Any) -> None:
    row = identity()
    origin = datetime(2020, 1, 1, tzinfo=UTC)
    charges = [QuotaCharge("write", 60, origin, 1), QuotaCharge("day", 86_400, origin, 10)]
    async with tx(store, store_space) as transaction:
        assert await transaction.insert_identity(row)
        first = await transaction.charge_quotas(row.id, charges, operation_key="op-1", kind="write")
        assert first.ok and not first.duplicate
        duplicate = await transaction.charge_quotas(row.id, charges, operation_key="op-1", kind="write")
        assert duplicate.duplicate
        exhausted = await transaction.charge_quotas(row.id, charges, operation_key="op-2", kind="write")
        assert not exhausted.ok
        usage = await transaction.quota_usage(row.id)
        assert {(item.kind, item.used) for item in usage} == {("day", 1), ("write", 1)}


async def test_external_event_cursor_advances_only_after_success(store: Store, store_space: Any) -> None:
    row = identity()
    async with tx(store, store_space) as transaction:
        assert await transaction.insert_identity(row)
        await transaction.emit([EventDraft("identity.registered", "test", service="svc", identity_id=row.id)])

    calls = 0

    async def failing(events: list[Any]) -> None:
        nonlocal calls
        calls += 1
        assert len(events) == 1
        raise RuntimeError("handler")

    with pytest.raises(RuntimeError, match="handler"):
        await store.process_events("contract", failing, limit=10)

    received: list[Any] = []

    async def successful(events: list[Any]) -> None:
        received.extend(events)

    assert await store.process_events("contract", successful, limit=10) == 1
    assert await store.process_events("contract", successful, limit=10) == 0
    assert calls == 1
    assert received[0].sequence == 1


async def test_search_uses_common_filters(store: Store, store_space: Any) -> None:
    from identity_hub.store.base import SearchFilter

    first = identity("alpha")
    second = identity("beta")
    async with tx(store, store_space) as transaction:
        assert await transaction.insert_identity(first)
        assert await transaction.insert_identity(second)
        rows, count = await transaction.search_identities(
            SearchFilter(services=frozenset({"svc"}), tags=frozenset({"worker"}), terms=("alpha",)),
            limit=10,
            after=None,
            count_limit=100,
        )
    assert [row.id for row in rows] == [first.id]
    assert count == 1


async def test_memory_clock_drives_expiry() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryStore(clock)
    space = await store.ensure_space("clock")
    row = identity()
    async with store.transaction(space) as transaction:
        assert await transaction.insert_identity(row)
        current = await transaction.get_identity(row.id)
        assert current is not None
        lease, _ = await transaction.acquire_lease(
            current,
            pool="default",
            holder="worker",
            ttl_seconds=5,
            idempotency_key=None,
            shared=False,
        )
    clock.advance(timedelta(seconds=5))
    async with store.transaction(space) as transaction:
        assert await transaction.current_lease(row.id) is None
        assert await transaction.expire_leases(10) == 1
        history = await transaction.lease_history(row.id, limit=1, before=None)
    assert history[0].released_at == lease.expires_at
    assert history[0].end_reason == "expired"
