"""`space.maintenance` — операции обслуживания пространства (IMP-23).

Обычно их вызывает диспетчер (`hub.maintenance`); вручную — для отладки и после
изменения `searchable_profile` сервиса (`reindex_search`).
"""

from __future__ import annotations

from identity_hub.api._base import Namespace

__all__ = ["SpaceMaintenance"]


class SpaceMaintenance(Namespace):
    """Обслуживание пространства: `space.maintenance`."""

    __slots__ = ()

    async def expire_challenges(self, *, limit: int = 100) -> int:
        """Закрыть просроченные challenge: auth → no_session, событие `identity.challenge.expired`."""
        return await self._space._expire_challenges(limit=limit)

    async def expire_logins(self, *, limit: int = 100) -> int:
        """Вернуть в `no_session` аккаунты, застрявшие в `logging_in` без аренды дольше срока входа."""
        return await self._space._expire_logins(limit=limit)

    async def expire_verifications(self, *, limit: int = 100) -> int:
        """Истёкшие сроки: verified → expired, pending → missing (ТЗ §8.9, обслуживание)."""
        return await self._space._expire_verifications(limit=limit)

    async def reindex_search(self, service: str, *, batch_size: int = 500) -> int:
        """Пересчитать search_text после изменения searchable-полей сервиса (FR-11.8)."""
        return await self._space._reindex_search(service, batch_size=batch_size)

    async def cleanup_proxy_allocations(self, *, limit: int = 100, group: str | None = None) -> int:
        """По limit ledger/proxy/history в трёх своих TX, без ожидания занятых строк."""
        closed, _ = await self._space.hub._proxy_recovery.run_and_repair(
            self._space, limit=limit, group=group
        )
        await self._space.hub._proxy_recovery.close_orphan_history(self._space, limit=limit, group=group)
        return closed
