"""Общий набор из пакета прогоняется против MemoryStore и PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.clock import FakeClock
from identity_hub.postgres.store import PgStore
from identity_hub.store.base import Store
from identity_hub.testing import MemoryStore
from identity_hub.testing.conformance import StoreConformanceSuite

pytestmark = [pytest.mark.conformance]


class TestStoreConformance(StoreConformanceSuite):
    @pytest.fixture
    async def store_under_test(
        self, backend_sessionmaker: async_sessionmaker[AsyncSession] | None
    ) -> AsyncIterator[Store]:
        if backend_sessionmaker is None:
            yield MemoryStore(FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
        else:
            yield PgStore(backend_sessionmaker)


async def test_memory_clock_drives_expiry() -> None:
    from identity_hub.store.base import NewIdentity

    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    store = MemoryStore(clock)
    space = await store.ensure_space("clock")
    row = NewIdentity(
        id=uuid4(),
        service="svc",
        external_ref="clock",
        lifecycle="ready",
        standing="good",
        auth_state="authenticated",
        tags=[],
        attributes={},
        profile={},
        labels={},
        note=None,
        search_text="clock",
    )
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
