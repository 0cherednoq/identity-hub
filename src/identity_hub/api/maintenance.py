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

    async def expire_verifications(self, *, limit: int = 100) -> int:
        """Истёкшие сроки: verified → expired, pending → missing (ТЗ §8.9, обслуживание)."""
        return await self._space._expire_verifications(limit=limit)

    async def reindex_search(self, service: str, *, batch_size: int = 500) -> int:
        """Пересчитать search_text после изменения searchable-полей сервиса (FR-11.8)."""
        return await self._space._reindex_search(service, batch_size=batch_size)
