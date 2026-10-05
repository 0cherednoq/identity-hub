"""Наследуемый pytest-набор контракта Store.

Пользователь объявляет класс ``TestStore(StoreConformanceSuite)`` и переопределяет fixture
``store_under_test``. Один и тот же набор затем проверяет сторонний адаптер и эталонные реализации.

Контракт `Store` внутренний и до версии 1.0 нестабилен (D-152): методы добавляются и меняются
вместе с библиотекой. Набор проверяет главные гарантии — изоляцию пространств, аренду и fencing,
атомарность квот, откат транзакции, курсор поиска и журнал событий, — но не каждый метод.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from identity_hub.crypto.envelope import EncryptedPayload
from identity_hub.domain.idempotency import fingerprint, namespace
from identity_hub.domain.regions import RegionState
from identity_hub.errors import IdempotencyConflict
from identity_hub.events import EventDraft
from identity_hub.store.base import (
    CandidateQuery,
    IdempotencyRequest,
    LeaseCounters,
    NewIdentity,
    QuotaCharge,
    SearchFilter,
    ServiceRow,
    SpaceRef,
    Store,
    StoreTx,
)

__all__ = ["StoreConformanceSuite"]


def _idempotency(space: SpaceRef, key: str, *, owner: UUID | None = None) -> IdempotencyRequest:
    return IdempotencyRequest(
        namespace=namespace(space.id, "operation"),
        key=key,
        operation="write",
        fingerprint=fingerprint("operation", {"kind": "write"}),
        ttl=timedelta(days=1),
        owner_identity_id=owner,
    )


def _identity(external_ref: str = "account-1", *, identity_id: UUID | None = None) -> NewIdentity:
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
async def _tx(store: Store, space: SpaceRef) -> AsyncIterator[StoreTx]:
    async with store.transaction(space) as transaction:
        yield transaction


class StoreConformanceSuite:
    """Обязательные сценарии для реализации Store."""

    @pytest.fixture
    async def store_under_test(self) -> AsyncIterator[Store]:
        """Переопределите fixture в наследнике и верните проверяемый Store."""

        raise NotImplementedError("определите fixture store_under_test")
        yield  # pragma: no cover

    @pytest.fixture
    async def conformance_space(self, store_under_test: Store) -> SpaceRef:
        await store_under_test.sync_services([ServiceRow("svc", "Service")])
        return await store_under_test.ensure_space("conformance", "Conformance")

    async def test_transaction_rolls_back(self, store_under_test: Store, conformance_space: SpaceRef) -> None:
        kept = _identity("kept")
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(kept)
        row = _identity()
        with pytest.raises(RuntimeError, match="rollback"):
            async with _tx(store_under_test, conformance_space) as transaction:
                assert await transaction.insert_identity(row)
                await transaction.emit(
                    [EventDraft("identity.registered", "test", service="svc", identity_id=row.id)]
                )
                stored = await transaction.get_identity(kept.id)
                assert stored is not None
                lease, _ = await transaction.acquire_lease(
                    stored, pool="p", holder="h", ttl_seconds=30, idempotency_key=None, shared=False
                )
                assert lease.fencing == 1
                raise RuntimeError("rollback")
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.get_identity(row.id) is None
            assert await transaction.current_fencing(kept.id) == 0  # аренда откатилась вместе с аккаунтом
            assert await transaction.current_lease(kept.id) is None

        async def nothing_expected(events: list[Any]) -> None:
            raise AssertionError(f"событие отменённой транзакции доставлено: {events}")

        assert not await store_under_test.process_events("rollback", nothing_expected, limit=10)

    async def test_spaces_are_isolated(self, store_under_test: Store, conformance_space: SpaceRef) -> None:
        """Аккаунт одного пространства не виден и не меняется из другого."""
        other = await store_under_test.ensure_space("conformance-other", "Other")
        row = _identity()
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
        async with _tx(store_under_test, other) as transaction:
            assert await transaction.get_identity(row.id) is None
            assert not await transaction.update_identity(row.id, {"note": "чужое"}, {"note": "контракт"})
            rows, count = await transaction.search_identities(
                SearchFilter(services=frozenset({"svc"})), limit=10, after=None, count_limit=10
            )
            assert rows == [] and count == 0
            assert await transaction.pick_candidate(CandidateQuery("svc", frozenset({"good"}))) is None

    async def test_live_lease_excludes_candidate_until_release(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        row = _identity()
        query = CandidateQuery("svc", frozenset({"good"}))
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
            candidate = await transaction.pick_candidate(query)
            assert candidate is not None
            lease, _ = await transaction.acquire_lease(
                candidate, pool="p", holder="h", ttl_seconds=30, idempotency_key=None, shared=False
            )
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.pick_candidate(query) is None  # единственный аккаунт занят
            released = await transaction.release_lease(lease, "released", LeaseCounters.empty(), shared=False)
            assert released == "released"
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.pick_candidate(query) is not None

    async def test_revoked_lease_is_not_extended_and_fences_writes(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        row = _identity()
        payload = EncryptedPayload("k", b"dek", b"nonce", b"cipher")
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
            stored = await transaction.get_identity(row.id)
            assert stored is not None
            lease, _ = await transaction.acquire_lease(
                stored, pool="p", holder="h", ttl_seconds=30, idempotency_key=None, shared=False
            )
            renewal = await transaction.extend_lease(lease, 30, LeaseCounters.empty(), shared=False)
            assert renewal.expires_at is not None
            assert await transaction.put_session(row.id, "default", payload, fencing=lease.fencing)
            revoked = await transaction.revoke_lease(row.id)
            assert revoked is not None and revoked.lease_id == lease.lease_id
            assert await transaction.current_fencing(row.id) == lease.fencing + 1
            renewal = await transaction.extend_lease(lease, 30, LeaseCounters.empty(), shared=False)
            assert renewal.expires_at is None
            # запись с fencing отозванной аренды отклоняется: новая сессия записана более новой арендой
            assert await transaction.put_session(row.id, "default", payload, fencing=lease.fencing + 1)
            assert not await transaction.put_session(row.id, "default", payload, fencing=lease.fencing)

    async def test_identity_region_and_data_compare_and_set(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        row = _identity()
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
            assert not await transaction.insert_identity(row)
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

    async def test_lease_fencing_and_idempotency(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        row = _identity()
        async with _tx(store_under_test, conformance_space) as transaction:
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
            assert reclaimed is None and lease.fencing == 1
            assert await transaction.find_live_lease("lease-1") == lease
            assert await transaction.current_fencing(row.id) == 1
            first = await transaction.release_lease(lease, "released", LeaseCounters.empty(), shared=False)
            again = await transaction.release_lease(lease, "released", LeaseCounters.empty(), shared=False)
            assert (first, again) == ("released", "closed")  # повтор — no-op (D-213)

    async def test_quota_charge_is_atomic_and_idempotent(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        row = _identity()
        origin = datetime(2020, 1, 1, tzinfo=UTC)
        charges = [QuotaCharge("write", 60, origin, 1), QuotaCharge("day", 86_400, origin, 10)]
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
            op_1 = _idempotency(conformance_space, "op-1", owner=row.id)
            first = await transaction.charge_quotas(row.id, charges, idempotency=op_1)
            assert first.ok and not first.duplicate
            assert first.claim is not None and first.claim.claimed
            duplicate = await transaction.charge_quotas(row.id, charges, idempotency=op_1)
            assert duplicate.duplicate
            assert duplicate.claim is not None and not duplicate.claim.claimed
            op_2 = _idempotency(conformance_space, "op-2", owner=row.id)
            exhausted = await transaction.charge_quotas(row.id, charges, idempotency=op_2)
            assert not exhausted.ok
            usage = await transaction.quota_usage(row.id)
            assert {(item.kind, item.used) for item in usage} == {("day", 1), ("write", 1)}
            # исчерпанная квота ключ не заняла: он свободен для следующей попытки
            free = await transaction.claim_idempotency_key(op_2)
            assert (free.claimed, free.attempt) == (True, 1)

    async def test_quota_charge_is_all_or_nothing(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        """Исчерпано позднее окно — более раннее в списке не списывается."""
        row = _identity()
        origin = datetime(2020, 1, 1, tzinfo=UTC)
        charges = [QuotaCharge("day", 86_400, origin, 10), QuotaCharge("write", 60, origin, 1)]
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
            assert (await transaction.charge_quotas(row.id, charges)).ok
            assert not (await transaction.charge_quotas(row.id, charges)).ok
            usage = {item.kind: item.used for item in await transaction.quota_usage(row.id)}
            assert usage == {"day": 1, "write": 1}

    async def test_idempotency_key_claim_complete_replay_conflict(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        request = _idempotency(conformance_space, "job-1")
        async with _tx(store_under_test, conformance_space) as transaction:
            first = await transaction.claim_idempotency_key(request)
            assert (first.claimed, first.attempt, first.previous_outcome) == (True, 1, None)
            unknown = await transaction.claim_idempotency_key(request)
            assert (unknown.claimed, unknown.previous_outcome) == (False, None)

            await transaction.complete_idempotency_key(request.namespace, request.key, "failed")
            with pytest.raises(IdempotencyConflict):  # другая команда не занимает и упавший ключ
                await transaction.claim_idempotency_key(replace(request, fingerprint="f" * 64))
            retry = await transaction.claim_idempotency_key(request)
            assert (retry.claimed, retry.attempt, retry.previous_outcome) == (True, 2, "failed")

            await transaction.complete_idempotency_key(
                request.namespace,
                request.key,
                "succeeded",
                result_type="lease",
                result_version=1,
                result={"lease_id": "l-1"},
            )
            replay = await transaction.claim_idempotency_key(request)
            assert (replay.claimed, replay.attempt, replay.previous_outcome) == (False, 2, "succeeded")
            assert (replay.result_type, replay.result_version, replay.result) == (
                "lease",
                1,
                {"lease_id": "l-1"},
            )
            with pytest.raises(IdempotencyConflict):
                await transaction.claim_idempotency_key(replace(request, operation="other"))

            # другой namespace — независимый ключ; истёкший ключ свободен
            other = replace(request, namespace=f"{conformance_space.id}:other")
            assert (await transaction.claim_idempotency_key(other)).claimed
            expired = replace(request, key="job-2", ttl=timedelta(0))
            assert (await transaction.claim_idempotency_key(expired)).attempt == 1
            again = await transaction.claim_idempotency_key(replace(expired, operation="other"))
            assert (again.claimed, again.attempt, again.previous_outcome) == (True, 1, None)

    async def test_idempotency_key_released_by_rollback(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        request = _idempotency(conformance_space, "job-rollback")
        with pytest.raises(RuntimeError):
            async with _tx(store_under_test, conformance_space) as transaction:
                assert (await transaction.claim_idempotency_key(request)).claimed
                raise RuntimeError("rollback")
        async with _tx(store_under_test, conformance_space) as transaction:
            again = await transaction.claim_idempotency_key(request)
            assert (again.claimed, again.attempt) == (True, 1)

    async def test_external_event_cursor_advances_only_after_success(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        row = _identity()
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
            await transaction.emit(
                [EventDraft("identity.registered", "test", service="svc", identity_id=row.id)]
            )

        async def failing(events: list[Any]) -> None:
            assert len(events) == 1
            raise RuntimeError("handler")

        with pytest.raises(RuntimeError, match="handler"):
            await store_under_test.process_events("contract", failing, limit=10)
        received: list[Any] = []

        async def successful(events: list[Any]) -> None:
            received.extend(events)

        assert await store_under_test.process_events("contract", successful, limit=10) == 1
        assert await store_under_test.process_events("contract", successful, limit=10) == 0
        assert received[0].sequence == 1

    async def test_search_uses_common_filters(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        first = _identity("alpha")
        second = _identity("beta")
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(first)
            assert await transaction.insert_identity(second)
            rows, count = await transaction.search_identities(
                SearchFilter(
                    services=frozenset({"svc"}),
                    tags=frozenset({"worker"}),
                    terms=("alpha",),
                ),
                limit=10,
                after=None,
                count_limit=100,
            )
        assert [row.id for row in rows] == [first.id]
        assert count == 1

    async def test_search_pages_with_cursor(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        rows_in = [_identity(f"page-{n}") for n in range(3)]
        async with _tx(store_under_test, conformance_space) as transaction:
            for row in rows_in:
                assert await transaction.insert_identity(row)
            seen: list[UUID] = []
            after: tuple[Any, UUID] | None = None
            for _ in range(5):
                page, _total = await transaction.search_identities(
                    SearchFilter(terms=("page",)), limit=1, after=after, count_limit=None
                )
                if not page:
                    break
                seen.append(page[0].id)
                after = (page[0].created_at, page[0].id)
        assert sorted(seen) == sorted(row.id for row in rows_in)
        assert len(seen) == len(set(seen)) == 3

    async def test_event_batches_skip_journal_and_respect_limit(
        self, store_under_test: Store, conformance_space: SpaceRef
    ) -> None:
        row = _identity()
        async with _tx(store_under_test, conformance_space) as transaction:
            assert await transaction.insert_identity(row)
            await transaction.emit(
                [
                    EventDraft("identity.registered", "test", service="svc", identity_id=row.id),
                    EventDraft("lease.acquired", "test", service="svc", identity_id=row.id),  # только журнал
                    EventDraft("identity.updated", "test", service="svc", identity_id=row.id),
                    EventDraft("identity.updated", "test", service="svc", identity_id=row.id),
                ]
            )
        batches: list[list[str]] = []

        async def collect(events: list[Any]) -> None:
            batches.append([event.type for event in events])

        assert await store_under_test.process_events("limits", collect, limit=2) == 2
        assert await store_under_test.process_events("limits", collect, limit=2) == 1
        assert await store_under_test.process_events("limits", collect, limit=2) == 0
        assert batches == [["identity.registered", "identity.updated"], ["identity.updated"]]
