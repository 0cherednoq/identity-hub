"""`space.analytics` — аналитические отчёты из идемпотентных rollups и снимков (IMP-23)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from identity_hub.api._base import Namespace

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from identity_hub.read.analytics import (
        AnalyticsSeries,
        BanRateReport,
        CohortReport,
        FunnelReport,
        ProxyQualityReport,
        TimeToReport,
        UtilizationReport,
    )
    from identity_hub.rollups import BucketSize

__all__ = ["Analytics"]


class Analytics(Namespace):
    """Аналитика пространства: `space.analytics`."""

    __slots__ = ()

    async def ban_rate(
        self, *, start: datetime, end: datetime, group_by: str, service: str | None = None
    ) -> BanRateReport:
        """Доля банов за период с группировкой `group_by`."""
        return await self._space._analytics_ban_rate(start=start, end=end, group_by=group_by, service=service)

    async def cohorts(
        self,
        *,
        start: datetime,
        end: datetime,
        offsets: Sequence[int] = (1, 7, 14, 30),
        service: str | None = None,
    ) -> CohortReport:
        """Когорты по дню регистрации: доля живых на смещениях `offsets` (дни)."""
        return await self._space._analytics_cohorts(start=start, end=end, offsets=offsets, service=service)

    async def funnel(self, *, start: datetime, end: datetime, service: str | None = None) -> FunnelReport:
        """Воронка: регистрация → вход → ready → первая аренда."""
        return await self._space._analytics_funnel(start=start, end=end, service=service)

    async def proxy_quality(self, *, start: datetime, end: datetime) -> ProxyQualityReport:
        """Качество прокси за период: ошибки, карантины, привязки."""
        return await self._space._analytics_proxy_quality(start=start, end=end)

    async def series(
        self,
        metric: str,
        *,
        start: datetime,
        end: datetime,
        bucket_size: BucketSize,
        group_by: str | None = None,
        dimensions: Mapping[str, str] | None = None,
    ) -> AnalyticsSeries:
        """Временной ряд метрики из сохранённых корзин 1m/1h/1d."""
        return await self._space._analytics_series(
            metric, start=start, end=end, bucket_size=bucket_size, group_by=group_by, dimensions=dimensions
        )

    async def time_to(
        self, metric: str, *, start: datetime, end: datetime, service: str | None = None
    ) -> TimeToReport:
        """Распределение времени до события (`metric`), например до первого бана."""
        return await self._space._analytics_time_to(metric, start=start, end=end, service=service)

    async def utilization(
        self, *, start: datetime, end: datetime, service: str | None = None
    ) -> UtilizationReport:
        """Загрузка парка: доля времени identity под арендой."""
        return await self._space._analytics_utilization(start=start, end=end, service=service)
