"""`space.sync` — сверка регионов identity с платформой через `Service.probes` (IMP-23).

Пробы выполняются под арендой (служебный пул `identity-hub:sync`) как операция `probe`.
Библиотека не запускает сверку сама: вызывайте `refresh_many` по расписанию.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from identity_hub.api._base import Namespace
from identity_hub.service import Service
from identity_hub.sync import ReconcileReport

__all__ = ["RefreshResult", "Sync"]


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Итог `sync.refresh`: исход по каждому региону (`applied`, `unchanged`, `sticky`…)."""

    identity_id: UUID
    outcomes: dict[str, str]


class Sync(Namespace):
    """Синхронизация статусов: `space.sync`."""

    __slots__ = ()

    async def refresh(
        self, identity_id: UUID, regions: Iterable[str] = ("auth", "standing")
    ) -> RefreshResult:
        """Внеочередная проба одного identity; исход по региону (`applied`, `unchanged`, `sticky`…).

        Регионы: `auth`, `standing`, `verification` (все факторы), `verification:<factor>`.
        Занятый identity ждёт освобождения до 30 секунд.
        """
        return RefreshResult(identity_id, await self._space._resync(identity_id, regions))

    async def refresh_many(
        self,
        regions: Iterable[str] = ("auth", "standing"),
        *,
        service: Service | str | None = None,
        not_synced_for: timedelta = timedelta(hours=6),
        limit: int = 50,
    ) -> ReconcileReport:
        """Фоновая сверка identity, чей первый регион не сверялся дольше `not_synced_for`.

        Занятые identity пропускаются (`skipped_busy`), сервисы без `probes` — тоже.
        """
        return await self._space._reconcile(
            regions, service=service, not_synced_for=not_synced_for, limit=limit
        )
