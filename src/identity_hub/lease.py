"""Аренда: эксклюзивное право воркера работать с аккаунтом (ТЗ §3.6, FR-6, G-1).

Аренда держится записью с TTL и heartbeat (FR-6.2), а не открытой транзакцией или advisory lock.
Публичный `lease_id` (UUIDv7), приватный `token` (никогда не логируется, FR-6.4), монотонный `fencing`.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from identity_hub.errors import LeaseUnavailable
from identity_hub.events import EventDraft
from identity_hub.store.base import IdentityRow, LeaseRow

if TYPE_CHECKING:
    from identity_hub.pool import Pool

__all__ = ["Lease", "acquire"]


class Lease:
    """Захваченная аренда. Жизненный цикл (heartbeat, release, самоограждение) — P3.3."""

    def __init__(self, pool: Pool, row: LeaseRow, identity: IdentityRow) -> None:
        self.pool = pool
        self._row = row
        self._token = row.token  # приватный: только для продления и освобождения
        self.identity = identity

    @property
    def lease_id(self) -> UUID:
        return self._row.lease_id

    @property
    def identity_id(self) -> UUID:
        return self._row.identity_id

    @property
    def fencing(self) -> int:
        return self._row.fencing

    @property
    def expires_at(self) -> datetime:
        return self._row.expires_at

    @property
    def acquired_at(self) -> datetime:
        return self._row.acquired_at

    @property
    def service(self) -> str:
        return self.pool.service.name

    @property
    def holder(self) -> str:
        return self._row.holder

    def __repr__(self) -> str:
        return (
            f"Lease(lease_id={self.lease_id}, identity_id={self.identity_id}, "
            f"pool={self.pool.name!r}, fencing={self.fencing})"
        )


async def acquire(
    pool: Pool,
    *,
    for_operation: str | None = None,
    tags: Iterable[str] = (),
    labels: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    among: Any = None,
    exclude: Iterable[UUID] = (),
    wait: timedelta | float | None = None,
    idempotency_key: str | None = None,
) -> Lease:
    """Выдать аккаунт в аренду (FR-6.1). Короткая транзакция: выбор SKIP LOCKED + запись аренды + история.

    `wait` — ждать освобождения подходящего аккаунта; по истечении — `LeaseUnavailable`.
    Повтор с тем же `idempotency_key`, пока аренда жива, возвращает ту же аренду (FR-6.5).
    """
    query = pool.candidate_query(
        for_operation=for_operation, tags=tags, labels=labels, profile=profile, among=among, exclude=exclude
    )
    space = pool.space
    clock = space.hub.clock
    wait_seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait or 0)
    deadline = clock.monotonic() + wait_seconds
    delay = 0.05
    while True:
        async with space._tx(own=True) as tx:
            if idempotency_key is not None:
                existing = await tx.find_live_lease(idempotency_key)
                if existing is not None:
                    identity = await space._identity(tx, existing.identity_id)
                    return Lease(pool, existing, identity)
            candidate = await tx.pick_candidate(query)
            if candidate is not None:
                row, reclaimed = await tx.acquire_lease(
                    candidate,
                    pool=pool.name,
                    holder=pool.holder,
                    ttl_seconds=pool.timing.ttl.total_seconds(),
                    idempotency_key=idempotency_key,
                    shared=pool.shared,
                )
                drafts = []
                if reclaimed is not None:
                    drafts.append(
                        _lease_event("lease.expired", pool, reclaimed, {"expired_at": reclaimed.expires_at})
                    )
                drafts.append(_lease_event("lease.acquired", pool, row, {"shared": pool.shared}))
                await space._emit(tx, *drafts)
                return Lease(pool, row, candidate)
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise LeaseUnavailable(f"пул {pool.name}: нет свободного аккаунта")
        await asyncio.sleep(min(remaining, delay * (0.5 + random.random())))
        delay = min(delay * 2, 2.0)


def _lease_event(event_type: str, pool: Pool, row: LeaseRow, extra: Mapping[str, Any]) -> EventDraft:
    return EventDraft(
        event_type,
        actor=row.holder,
        service=pool.service.name,
        identity_id=row.identity_id,
        lease_id=row.lease_id,
        data={"pool": row.pool, "holder": row.holder, "fencing": row.fencing, **extra},
    )
