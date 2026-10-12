"""Намеренно сломанные Store для проверки, что `StoreConformanceSuite` их ловит (F5.3a, R2-P04).

Каждый вариант — `MemoryStore` с одним дефектом. Набор, который не падает на варианте, слишком слаб.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from identity_hub.clock import FakeClock
from identity_hub.store.base import CandidateQuery, IdentityRow, LeaseRenewal, QuotaCharge, SearchFilter
from identity_hub.testing import MemoryStore
from identity_hub.testing.memory import MemoryTx

MUTATIONS = {
    "isolation": "аккаунты видны из любого пространства",
    "live_lease": "выборка кандидата не учитывает живую аренду",
    "revoke": "отозванная аренда продлевается, отзыв ничего не делает",
    "fencing": "сессия записывается без проверки fencing",
    "quota_atomic": "квоты списываются по одному окну, без отката",
    "search_cursor": "поиск игнорирует курсор",
    "rollback": "откат транзакции возвращает только аккаунты",
    "events": "журнальные события доставляются потребителям, limit не соблюдается",
}


class _IsolationTx(MemoryTx):
    def _own(self, identity_id: UUID) -> IdentityRow | None:
        return self.state.identities.get(identity_id)


class _LiveLeaseTx(MemoryTx):
    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
        rows = [row for row in self.state.identities.values() if row.space_id == self.space.id]
        return rows[0] if rows else None


class _RevokeTx(MemoryTx):
    async def revoke_lease(self, identity_id: UUID) -> Any:
        lease = self.state.leases.get(identity_id)
        return lease

    async def extend_lease(
        self, lease: Any, ttl_seconds: float, counters: Any, *, shared: bool
    ) -> LeaseRenewal:
        return LeaseRenewal(self._now)


class _FencingTx(MemoryTx):
    async def put_session(
        self, identity_id: UUID, slot: str, payload: Any, *, fencing: int, expires_at: datetime | None = None
    ) -> bool:
        self.state.sessions[(identity_id, slot)] = (payload, "valid", fencing, expires_at)
        return True


class _QuotaTx(MemoryTx):
    async def charge_quotas(
        self, identity_id: UUID, charges: Sequence[QuotaCharge], *, idempotency: Any = None
    ) -> Any:
        outcome = None
        for index, charge in enumerate(charges):
            outcome = await super().charge_quotas(
                identity_id, [charge], idempotency=idempotency if index == 0 else None
            )
            if not outcome.ok:
                return outcome
        return outcome if outcome is not None else await super().charge_quotas(identity_id, charges)


class _SearchTx(MemoryTx):
    async def search_identities(
        self, flt: SearchFilter, *, limit: int, after: Any, count_limit: int | None
    ) -> tuple[list[IdentityRow], int | None]:
        return await super().search_identities(flt, limit=limit, after=None, count_limit=count_limit)


_TX: dict[str, type[MemoryTx]] = {
    "isolation": _IsolationTx,
    "live_lease": _LiveLeaseTx,
    "revoke": _RevokeTx,
    "fencing": _FencingTx,
    "quota_atomic": _QuotaTx,
    "search_cursor": _SearchTx,
}


class _BrokenStore(MemoryStore):
    def __init__(self, mutation: str) -> None:
        super().__init__(FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))
        self.mutation = mutation

    @asynccontextmanager
    async def transaction(self, space: Any, *, session: Any | None = None) -> AsyncIterator[MemoryTx]:
        if self.mutation == "rollback":
            async with self._lock:
                identities = dict(self._state.identities)
                tx = MemoryTx(self, space, self.clock.now())
                try:
                    yield tx
                except BaseException:
                    self._state.identities = identities
                    raise
            return
        async with super().transaction(space, session=session) as tx:
            tx.__class__ = _TX.get(self.mutation, MemoryTx)
            yield tx

    async def process_events(
        self,
        consumer: str,
        handler: Callable[[list[Any]], Awaitable[int | None]],
        *,
        limit: int,
        external_only: bool = True,
    ) -> int | None:
        if self.mutation != "events":
            return await super().process_events(consumer, handler, limit=limit, external_only=external_only)
        cursor = self._state.cursors.get(consumer, 0)
        events = [event for event, _space, _external in self._state.events if event.id > cursor]
        if not events:
            return 0
        await handler(events)
        self._state.cursors[consumer] = events[-1].id
        return len(events)


def make_broken(mutation: str) -> MemoryStore:
    assert mutation in MUTATIONS, mutation
    return _BrokenStore(mutation)
