"""Мутационная проверка: намеренно неверный Store против StoreConformanceSuite."""

from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from identity_hub.clock import FakeClock
from identity_hub.store.base import ChargeOutcome, LeaseHistoryRow, LeaseRow, QuotaWindow
from identity_hub.testing import MemoryStore, MemoryTx
from identity_hub.testing.conformance import StoreConformanceSuite

BROKEN = [
    "нет изоляции пространств (get_identity/_own отдают чужие аккаунты)",
    "pick_candidate игнорирует живую аренду, standing, auth_state, lifecycle, availability и LRU",
    "acquire_lease перезаписывает чужую живую аренду",
    "extend_lease всегда успешен, revoke_lease и expire_leases ничего не делают",
    "put_session не проверяет fencing",
    "charge_quotas не атомарен и окно квоты никогда не сменяется",
    "search_identities ищет по всем пространствам, без сортировки и без курсора",
    "откат транзакции возвращает только аккаунты (аренды, квоты, события остаются)",
    "process_events отдаёт и внутренние события, игнорирует limit",
]


class BrokenTx(MemoryTx):
    def _own(self, identity_id):
        return self.state.identities.get(identity_id)  # 1

    async def pick_candidate(self, query):  # 2
        rows = [r for r in self.state.identities.values() if r.service == query.service and query.tags <= set(r.tags)]
        return rows[0] if rows else None

    async def acquire_lease(self, identity, *, pool, holder, ttl_seconds, idempotency_key, shared, proxy_id=None):  # 3
        current = self.state.identities[identity.id]
        fencing = current.fencing_seq + 1
        self.state.identities[identity.id] = replace(current, fencing_seq=fencing)
        lease = LeaseRow(uuid4(), uuid4(), identity.id, fencing, pool, holder, self._now,
                         self._now + timedelta(seconds=ttl_seconds), idempotency_key, proxy_id, self._now)
        self.state.leases[identity.id] = lease
        self.state.history[lease.lease_id] = LeaseHistoryRow(
            lease.lease_id, identity.id, fencing, pool, holder, proxy_id, self._now, self._now, None, None, None, {}, 0, 0, None
        )
        return lease, None

    async def extend_lease(self, lease, ttl_seconds, counters, *, shared):  # 4
        return self._now + timedelta(seconds=ttl_seconds)

    async def revoke_lease(self, identity_id):
        return None

    async def expire_leases(self, limit):
        return 0

    async def put_session(self, identity_id, slot, payload, *, fencing, expires_at=None):  # 5
        self.state.sessions[(identity_id, slot)] = (payload, "valid", fencing, expires_at)
        return True

    async def charge_quotas(self, identity_id, charges, *, operation_key, kind):  # 6
        if operation_key is not None and operation_key in self.state.operation_keys:
            return ChargeOutcome(duplicate=True)
        if operation_key is not None:
            self.state.operation_keys[operation_key] = (identity_id, kind, None, self._now)
        for charge in charges:
            key = (identity_id, charge.kind, charge.window_seconds, datetime(2100, 1, 1, tzinfo=UTC))
            used = self.state.quotas.get(key, 0)
            if used >= charge.limit:
                return ChargeOutcome(exhausted=QuotaWindow(charge.kind, charge.window_seconds, key[3], used))
            self.state.quotas[key] = used + 1  # уже записано, отката нет
        return ChargeOutcome()

    async def quota_usage(self, identity_id):
        return [QuotaWindow(k, s, start, used) for (o, k, s, start), used in self.state.quotas.items() if o == identity_id]

    async def search_identities(self, flt, *, limit, after, count_limit):  # 7
        rows = [r for r in self.state.identities.values()
                if all(t.casefold() in r.search_text.casefold() for t in flt.terms)]
        return rows[:limit], len(rows)


class BrokenStore(MemoryStore):
    @asynccontextmanager
    async def transaction(self, space, *, session=None):
        async with self._lock:
            identities = deepcopy(self._state.identities)
            tx = BrokenTx(self, space, self.clock.now())
            try:
                yield tx
            except BaseException:
                self._state.identities = identities  # 8
                raise

    async def process_events(self, consumer, handler, *, limit, external_only=True):  # 9
        cursor = self._state.cursors.get(consumer, 0)
        events = [event for event, _, _ in self._state.events if event.id > cursor]
        if not events:
            return 0
        await handler(events)
        self._state.cursors[consumer] = events[-1].id
        return len(events)


class TestBrokenStore(StoreConformanceSuite):
    @pytest.fixture
    async def store_under_test(self):
        yield BrokenStore(FakeClock(datetime(2026, 1, 1, tzinfo=UTC)))


def test_report_mutations():
    print("\nнамеренные дефекты BrokenStore:")
    for item in BROKEN:
        print("  -", item)
