"""Воркер операций провайдера (IMP-11, D-223): восстановление истёкших claim без сетевого I/O.

Вход и пробы выполняются там, где их запросили (`auth.login`, `sync.refresh`), и автоматически не
повторяются: ошибка — `failed`, следующую попытку делает вызывающий код. Воркер переводит `running`
с истёкшим claim (процесс упал или завис между claim и итогом) в `failed`; поздний ответ такого
процесса отвергается claim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from identity_hub.domain.provider_operations import RetryPolicy

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["NO_RETRY", "ProviderOperationsWorker"]

NO_RETRY = RetryPolicy(max_attempts=1)  # вход и пробы не повторяются автоматически (D-223)


class ProviderOperationsWorker:
    """`run_once(limit)` — один проход восстановления по всем пространствам; счётчик `recovered`."""

    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub
        self.recovered = 0

    async def run_once(self, limit: int = 100) -> int:
        if limit <= 0:
            return 0
        total = 0
        for ref in await self.hub._store.maintenance_spaces():
            if total >= limit:
                break
            space = self.hub.space(ref.slug, ref.title, space_id=ref.id)
            async with space._tx(own=True) as tx:
                total += await tx.recover_provider_operations(limit=limit - total, policy=NO_RETRY)
        self.recovered += total
        return total
