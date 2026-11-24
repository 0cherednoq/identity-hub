"""Ограниченный обход ledger: отдельные короткие транзакции, canonical lease и повтор пропусков."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub.errors import ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.proxy_allocator import close_use, reconcile_counters

if TYPE_CHECKING:
    from identity_hub._space import SpaceBase
    from identity_hub.hub import IdentityHub


@dataclass(frozen=True, slots=True)
class Cursor:
    after: UUID | None = None
    until: UUID | None = None


class ProxyRecovery:
    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub
        self._cursors: dict[tuple[UUID, str | None], tuple[Cursor, int]] = {}
        self._counter_cursors: dict[tuple[UUID, str | None], tuple[Cursor, int]] = {}
        self._history_cursors: dict[tuple[UUID, str | None], tuple[Cursor, int]] = {}

    async def close_orphan_history(self, space: SpaceBase, *, limit: int, group: str | None = None) -> int:
        _validate(limit, group)
        ref = await space.ref()
        key = (ref.id, group)
        previous = self._history_cursors.get(key, (Cursor(), 0))
        cursor, generation = previous
        async with self.hub._store.transaction(ref) as tx:
            rows, upper = await tx.proxy_history_page(limit, after=cursor.after, until=cursor.until)
            following = Cursor(rows[-1].lease_id, upper) if rows and rows[-1].lease_id != upper else Cursor()
            selected = [row for row in rows if row.proxy_allocation_id is not None]
            if group is not None:
                proxies = {
                    proxy_id: await tx.get_proxy(proxy_id)
                    for proxy_id in sorted({row.proxy_id for row in selected if row.proxy_id is not None})
                }
                selected = [
                    row
                    for row in selected
                    if row.proxy_id is not None
                    and (proxy := proxies[row.proxy_id]) is not None
                    and proxy.pool == group
                ]
            closed = await tx.proxy_close_orphan_history(selected)
            for row, identity_locked in closed:
                identity = await tx.get_identity(row.identity_id) if identity_locked else None
                tx.defer_events(
                    [
                        EventDraft(
                            "lease.lost",
                            actor="maintenance",
                            service=identity.service if identity else None,
                            identity_id=row.identity_id if identity_locked else None,
                            proxy_id=row.proxy_id,
                            lease_id=row.lease_id,
                            data={"pool": row.pool, "reason": "orphaned"},
                        )
                    ]
                )
        if self._history_cursors.get(key, (Cursor(), 0)) == previous:
            self._history_cursors[key] = (following, generation + 1)
        return len(closed)

    async def run_and_repair(
        self, space: SpaceBase, *, limit: int, group: str | None = None, managed_only: bool = False
    ) -> tuple[int, int]:
        repaired = await self.repair_counters(space, limit=limit, group=group, managed_only=managed_only)
        closed = await self.run(space, limit=limit, group=group, managed_only=managed_only)
        return closed, repaired

    async def run(
        self, space: SpaceBase, *, limit: int, group: str | None = None, managed_only: bool = False
    ) -> int:
        _validate(limit, group)
        ref = await space.ref()
        key = (ref.id, group)
        previous = self._cursors.get(key, (Cursor(), 0))
        cursor, generation = previous
        closed = 0
        async with self.hub._store.transaction(ref) as tx:
            if managed_only:
                config = await tx.proxy_group(group or "")
                if config is None or config.capacity_accounting != "allocations":
                    return 0
            rows, upper = await tx.proxy_cleanup_page(limit, after=cursor.after, until=cursor.until)
            following = Cursor(rows[-1].id, upper) if rows and rows[-1].id != upper else Cursor()
            selected = rows
            if group is not None:
                proxies = {
                    proxy_id: await tx.get_proxy(proxy_id)
                    for proxy_id in sorted({row.proxy_id for row in rows})
                }
                selected = [
                    row
                    for row in rows
                    if (proxy := proxies[row.proxy_id]) is not None and proxy.pool == group
                ]
            owned = await tx.proxy_cleanup_lock_rows(selected)
            for row in selected:
                if row.id not in owned:
                    continue
                if group is not None:
                    proxy = await tx.get_proxy(row.proxy_id)
                    if proxy is None or proxy.pool != group:
                        continue
                closed += int(await close_use(tx, row.id))
        # Commit прежде курсора; конкурентный проход не откатывает уже продвинутый курсор.
        if self._cursors.get(key, (Cursor(), 0)) == previous:
            self._cursors[key] = (following, generation + 1)
        return closed

    async def repair_counters(
        self, space: SpaceBase, *, limit: int, group: str | None = None, managed_only: bool = False
    ) -> int:
        _validate(limit, group)
        ref = await space.ref()
        key = (ref.id, group)
        previous = self._counter_cursors.get(key, (Cursor(), 0))
        cursor, generation = previous
        repaired = 0
        # Отдельная TX: после ledger-пачки нельзя брать ещё proxy с меньшими ID.
        async with self.hub._store.transaction(ref) as tx:
            if managed_only:
                config = await tx.proxy_group(group or "")
                if config is None or config.capacity_accounting != "allocations":
                    return 0
            rows, upper = await tx.proxy_counter_page(limit, after=cursor.after, until=cursor.until)
            following = Cursor(rows[-1][0], upper) if rows and rows[-1][0] != upper else Cursor()
            selected = [proxy_id for proxy_id, pool in rows if group is None or pool == group]
            owned = await tx.proxy_lock_rows_skip_locked(selected)
            for proxy_id in selected:
                if proxy_id not in owned:
                    continue
                before = await tx.get_proxy(proxy_id)
                if before is None or (group is not None and before.pool != group):
                    continue
                config = await tx.proxy_group(before.pool)
                if config is None or config.capacity_accounting != "allocations":
                    continue
                await reconcile_counters(tx, proxy_id, fix=True)
                after = await tx.get_proxy(proxy_id)
                assert after is not None  # proxy-lock исключает delete
                repaired += int(
                    (before.bound_count, before.reserved_count, before.draining_count)
                    != (after.bound_count, after.reserved_count, after.draining_count)
                )
        if self._counter_cursors.get(key, (Cursor(), 0)) == previous:
            self._counter_cursors[key] = (following, generation + 1)
        return repaired


def _validate(limit: int, group: str | None) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValidationFailed("proxy cleanup limit must be from 1 to 1000")
    if group is not None and (not isinstance(group, str) or not 1 <= len(group) <= 128):
        raise ValidationFailed("proxy cleanup group must contain from 1 to 128 characters")
