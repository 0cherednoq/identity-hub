"""Координатор пакетного heartbeat процесса (IMP-02, D-211).

Аренды продлеваются по-прежнему каждая по своему расписанию, но сам запрос уходит пачкой: всё,
что пришло к ходу координатора, продлевается одним `UPDATE ... WHERE token = ANY(...)`. Координатор
живёт, пока есть очередь: первый запрос его запускает, пустая очередь завершает.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from identity_hub.store.base import Store

__all__ = ["BATCH_LIMIT", "HeartbeatBatcher"]

BATCH_LIMIT = 500  # токенов в одном UPDATE: дольше — длиннее блокировки строк пачки


class HeartbeatBatcher:
    """Очередь продлений: (token, ttl) → future с новым `expires_at` или `None`."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._pending: dict[float, list[tuple[UUID, asyncio.Future[datetime | None]]]] = defaultdict(list)
        self._task: asyncio.Task[None] | None = None

    @property
    def idle(self) -> bool:
        """Координатор не работает и очередь пуста."""
        return (self._task is None or self._task.done()) and not self._pending

    async def renew(self, token: UUID, ttl_seconds: float) -> datetime | None:
        """Продлить аренду в ближайшей пачке. `None` — аренды с этим token больше нет."""
        future: asyncio.Future[datetime | None] = asyncio.get_running_loop().create_future()
        self._pending[ttl_seconds].append((token, future))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ih-heartbeat-batcher")
        return await future

    async def _run(self) -> None:
        # один проход цикла событий: аренды, чьё время пришло одновременно, попадают в одну пачку
        await asyncio.sleep(0)
        while self._pending:
            ttl, queue = next(iter(self._pending.items()))
            batch, rest = queue[:BATCH_LIMIT], queue[BATCH_LIMIT:]
            if rest:
                self._pending[ttl] = rest
            else:
                del self._pending[ttl]
            live = [(token, future) for token, future in batch if not future.done()]
            if not live:
                continue
            try:
                renewed = await self._store.renew_leases([token for token, _ in live], ttl)
            except Exception as exc:
                # ошибка запроса — всем арендам пачки: каждая повторит по своему backoff (R1-03)
                for _, future in live:
                    if not future.done():
                        future.set_exception(exc)
                continue
            for token, future in live:
                if not future.done():
                    future.set_result(renewed.get(token))
