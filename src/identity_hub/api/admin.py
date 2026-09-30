"""`space.admin` — read-модели админки и аудит её действий (IMP-23).

Методы возвращают secret-free DTO для UI; `record_*` пишут события аудита
административных действий, не сохраняя их содержимого.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from identity_hub.api._base import Namespace

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from identity_hub.domain.query import SearchQuery
    from identity_hub.links import LinkSummary
    from identity_hub.pool import Pool
    from identity_hub.read.admin_state import ImportRunInfo, ProviderOperationInfo
    from identity_hub.read.cards import LeaseStats
    from identity_hub.read.deliveries import DeliveryCenter
    from identity_hub.read.events import EventFilters, EventItem, EventPage
    from identity_hub.read.human_queue import HumanQueuePage, QueueKind, QueueUrgency
    from identity_hub.read.leases import LeaseDashboard, LeaseFilters, LeaseGroup, LeaseHistoryPage
    from identity_hub.read.overview import PoolCapacity, SpaceOverview
    from identity_hub.read.proxies import ProxyDetail, ProxyFilters, ProxyPage
    from identity_hub.read.search import AccountPage, AccountSearchOptions, AccountSelectionSnapshot
    from identity_hub.store.base import LeaseHistoryRow

__all__ = ["Admin"]


class Admin(Namespace):
    """Read-модели и аудит админки: `space.admin`."""

    __slots__ = ()

    async def delivery_center(
        self,
        *,
        webhook_limit: int = 50,
        webhook_before: tuple[datetime, int] | None = None,
        notification_limit: int = 50,
        notification_before: tuple[datetime, int] | None = None,
    ) -> DeliveryCenter:
        """Сводка доставок вебхуков и уведомлений для UI; курсоры диспетчера не трогает."""
        return await self._space._delivery_center(
            webhook_limit=webhook_limit,
            webhook_before=webhook_before,
            notification_limit=notification_limit,
            notification_before=notification_before,
        )

    async def event_detail(self, event_id: UUID) -> EventItem:
        """Одно событие журнала пространства с полными данными."""
        return await self._space._event_detail(event_id)

    async def event_page(
        self,
        *,
        filters: EventFilters | None = None,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> EventPage:
        """Страница журнала событий с фильтрами (keyset-пагинация по `before`)."""
        return await self._space._event_page(filters=filters, limit=limit, before=before)

    async def human_queue(
        self,
        *,
        kind: QueueKind | None = None,
        service: str | None = None,
        urgency: QueueUrgency | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> HumanQueuePage:
        """Очередь ручных действий: challenge, верификации на ревью, требования платформы."""
        return await self._space._human_queue(
            kind=kind, service=service, urgency=urgency, limit=limit, after=after
        )

    async def identity_page(
        self,
        query: str | SearchQuery = "",
        *,
        options: AccountSearchOptions | None = None,
        limit: int = 50,
        after: str | None = None,
        count: bool = True,
    ) -> AccountPage:
        """Обогащённая страница identity для админки без секретов (бывш. `account_search`)."""
        return await self._space._account_search(
            query, options=options, limit=limit, after=after, count=count
        )

    async def import_runs(self, *, limit: int = 100) -> tuple[ImportRunInfo, ...]:
        """Последние фоновые прогоны импорта без входных данных и секретов."""
        return await self._space._import_runs(limit=limit)

    async def lease_dashboard(
        self, *, filters: LeaseFilters | None = None, group_by: LeaseGroup = "pool"
    ) -> LeaseDashboard:
        """Сводка аренд по окнам 15m/1h/6h/24h, срезы по пулам/сервисам/holder и живые аренды."""
        return await self._space._lease_dashboard(filters=filters, group_by=group_by)

    async def lease_history(
        self, identity_id: UUID, *, limit: int = 50, before: datetime | None = None
    ) -> list[LeaseHistoryRow]:
        """История аренд identity, новые первыми; без token."""
        return await self._space._lease_history(identity_id, limit=limit, before=before)

    async def lease_history_page(
        self,
        *,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
        filters: LeaseFilters | None = None,
    ) -> LeaseHistoryPage:
        """Страница истории аренд пространства с фильтрами."""
        return await self._space._lease_history_page(limit=limit, before=before, filters=filters)

    async def lease_stats(self, identity_id: UUID, since: datetime) -> LeaseStats:
        """Статистика аренд identity с момента `since`: число, длительность, загрузка."""
        return await self._space._lease_stats(identity_id, since)

    async def link_summary(self, ids: Sequence[UUID]) -> dict[UUID, LinkSummary]:
        """Сводка связей для страницы списка одним запросом."""
        return await self._space._link_summary(ids)

    async def overview(self, *, service: str | None = None) -> SpaceOverview:
        """Текущие счётчики пространства без секретов, опционально по одному сервису."""
        return await self._space._space_overview(service=service)

    async def pool_capacity(self, pool: Pool) -> PoolCapacity:
        """Ёмкость пула без генерации событий `pool.capacity.*`."""
        return await self._space._pool_capacity(pool)

    async def pool_summary(self, service: str | None = None) -> dict[str, dict[str, dict[str, int]]]:
        """Число identity по сервису и значениям регионов: service → регион → значение → n."""
        return await self._space._pool_summary(service)

    async def provider_operations(
        self, *, identity_id: UUID | None = None, states: frozenset[str] | None = None, limit: int = 100
    ) -> tuple[ProviderOperationInfo, ...]:
        """Состояние provider operations (claim/call/apply) без payload адаптера."""
        return await self._space._provider_operations(identity_id=identity_id, states=states, limit=limit)

    async def proxy_detail(
        self, proxy_id: UUID, *, history_limit: int = 50, history_before: tuple[datetime, UUID] | None = None
    ) -> ProxyDetail:
        """Карточка прокси: привязанные identity и история привязок."""
        return await self._space._proxy_detail(
            proxy_id, history_limit=history_limit, history_before=history_before
        )

    async def proxy_overview(
        self,
        *,
        filters: ProxyFilters | None = None,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> ProxyPage:
        """Страница прокси со статусами, загрузкой и ёмкостью групп."""
        return await self._space._proxy_overview(filters=filters, limit=limit, before=before)

    async def record_bulk(
        self,
        operation: str,
        *,
        actor: str,
        reason: str,
        snapshot_id: UUID,
        succeeded: int,
        failed: int,
        attempt: int,
    ) -> None:
        """Аудит одной попытки массового действия без данных identity."""
        await self._space._record_admin_bulk(
            operation,
            actor=actor,
            reason=reason,
            snapshot_id=snapshot_id,
            succeeded=succeeded,
            failed=failed,
            attempt=attempt,
        )

    async def record_export(
        self,
        kind: Literal["accounts", "events"],
        *,
        actor: str,
        reason: str,
        format: Literal["csv", "json"],
        count: int,
        filter_fingerprint: str,
    ) -> None:
        """Аудит завершённого экспорта без его содержимого."""
        await self._space._record_admin_export(
            kind,
            actor=actor,
            reason=reason,
            format=format,
            count=count,
            filter_fingerprint=filter_fingerprint,
        )

    async def record_import(
        self,
        action: Literal["started", "cancel_requested"],
        run_id: UUID,
        *,
        actor: str,
        reason: str,
        service: str,
        count: int | None = None,
    ) -> None:
        """Аудит жизненного цикла импорта без имён файлов и значений строк."""
        await self._space._record_admin_import(
            action, run_id, actor=actor, reason=reason, service=service, count=count
        )

    async def record_integration(
        self,
        action: str,
        resource_kind: Literal["notification_channel", "notification_rule", "webhook_endpoint", "delivery"],
        resource_id: str,
        *,
        actor: str,
        reason: str,
        outcome: str = "changed",
    ) -> None:
        """Аудит команды интеграции без адресов, payload, токенов и ключей подписи."""
        await self._space._record_admin_integration(
            action, resource_kind, resource_id, actor=actor, reason=reason, outcome=outcome
        )

    async def selection_snapshot(
        self, query: str | SearchQuery = "", *, options: AccountSearchOptions | None = None, limit: int = 1000
    ) -> AccountSelectionSnapshot:
        """Заморозить id и revision выборки для серверного bulk-действия.

        Бывш. `account_selection_snapshot`.
        """
        return await self._space._account_selection_snapshot(query, options=options, limit=limit)
