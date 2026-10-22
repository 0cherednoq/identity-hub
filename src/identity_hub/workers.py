"""Фоновые работы хаба одним ядром (`IMP-20`, `D-327`): `hub.workers.<name>.run_once(limit)`.

Библиотека не запускает ни потоков, ни задач: ни импорт пакета, ни создание `IdentityHub`, ни
`hub.start()`. Цикл принадлежит приложению — `hub.dispatcher.run()`/`running()` и команды CLI
сами являются циклами поверх `run_once`.

`limit` ограничивает работу там, где строки отбирает SQL (`process_events`, `stale_identities`,
`recover_provider_operations`, `list_acceptance`, `due_identity_timers`, `expire_leases`), а не
после загрузки всех строк. `WorkerRun` содержит только счётчики: ни id, ни секретов, ни текста
чужих исключений.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from identity_hub.acceptance import AcceptanceWorker
from identity_hub.errors import ValidationFailed

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["WorkerRun", "Workers"]


@dataclass(frozen=True, slots=True)
class WorkerRun:
    """Итог одного прохода фоновой работы (`D-327`).

    `processed` — сколько единиц работы выполнено, `counts` — та же работа в разрезе шагов,
    `truncated` — упёрлись в `limit`, то есть работа, вероятно, осталась.
    """

    worker: str
    limit: int
    processed: int = 0
    counts: Mapping[str, int] = field(default_factory=dict)
    truncated: bool = False

    def dump(self) -> dict[str, Any]:
        """JSON-совместимый вид для логов и метрик."""
        return {
            "worker": self.worker,
            "limit": self.limit,
            "processed": self.processed,
            "counts": dict(self.counts),
            "truncated": self.truncated,
        }


def _check_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValidationFailed("limit: целое число не меньше нуля")
    return limit


class Worker:
    """Общий фасад фоновой работы: `run_once(limit)` без состояния между проходами."""

    name = "worker"
    __slots__ = ("hub",)

    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub

    async def run_once(self, limit: int = 100) -> WorkerRun:
        """Один проход не больше `limit` единиц работы; `limit=0` — без обращения к базе."""
        _check_limit(limit)
        if limit == 0:
            return WorkerRun(self.name, limit)
        counts = await self._work(limit)
        processed = sum(counts.values())
        return WorkerRun(self.name, limit, processed, counts, processed >= limit)

    async def _work(self, limit: int) -> dict[str, int]:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{type(self).__name__}(worker={self.name!r})"


class DispatchWorker(Worker):
    """Одна пачка событий каждому потребителю и один проход очередей доставки."""

    name = "dispatch"
    __slots__ = ()

    async def _work(self, limit: int) -> dict[str, int]:
        return {"events": await self.hub.dispatcher.run_once(limit=limit)}


class MaintenanceWorker(Worker):
    """Задачи обслуживания, чей интервал наступил (таймеры, аренды, очистка)."""

    name = "maintenance"
    __slots__ = ()

    async def _work(self, limit: int) -> dict[str, int]:
        return {"tasks": await self.hub.maintenance.run_due(limit=limit)}


class ReconcileWorker(Worker):
    """Фоновая сверка регионов с платформой по всем пространствам (`space.sync.refresh_many`)."""

    name = "reconcile"
    __slots__ = ()

    async def _work(self, limit: int) -> dict[str, int]:
        probed = skipped = 0
        for ref in await self.hub._store.maintenance_spaces():
            remaining = limit - probed - skipped
            if remaining <= 0:
                break
            space = self.hub.space(ref.slug, ref.title, space_id=ref.id)
            report = await space.sync.refresh_many(limit=remaining)
            probed += len(report.probed)
            skipped += len(report.skipped_busy) + len(report.skipped_unavailable)
        return {"probed": probed, "skipped": skipped}


class ProviderOperationsFacade(Worker):
    """Возврат операций провайдера с истёкшим claim в `failed` (`D-223`)."""

    name = "provider_operations"
    __slots__ = ()

    async def _work(self, limit: int) -> dict[str, int]:
        return {"recovered": await self.hub._provider_worker.run_once(limit)}


class AcceptanceFacade(Worker):
    """Приёмка импортированных аккаунтов по сохранённой политике (`AcceptPolicy`)."""

    name = "acceptance"
    __slots__ = ()

    async def _work(self, limit: int) -> dict[str, int]:
        return {"accepted": await self.hub._acceptance.run_once(limit)}


class Workers:
    """Фоновые работы хаба: `hub.workers.dispatch`, `.maintenance`, `.reconcile`,
    `.provider_operations`, `.acceptance`. У каждой — `run_once(limit) -> WorkerRun`."""

    __slots__ = ("acceptance", "dispatch", "maintenance", "provider_operations", "reconcile")

    def __init__(self, hub: IdentityHub) -> None:
        self.dispatch = DispatchWorker(hub)
        self.maintenance = MaintenanceWorker(hub)
        self.reconcile = ReconcileWorker(hub)
        self.provider_operations = ProviderOperationsFacade(hub)
        self.acceptance = AcceptanceFacade(hub)

    def __iter__(self) -> Any:
        """Обойти все работы: `for worker in hub.workers: await worker.run_once(limit=100)`."""
        return iter(
            (
                self.dispatch,
                self.maintenance,
                self.reconcile,
                self.provider_operations,
                self.acceptance,
            )
        )

    def __repr__(self) -> str:
        return "Workers(dispatch, maintenance, reconcile, provider_operations, acceptance)"


# прежний движок приёмки остаётся доступным как `identity_hub.workers.AcceptanceWorker`
__all__ += ["AcceptanceWorker"]
