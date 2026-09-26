"""Независимый от Prometheus хук метрик (ТЗ §10.2)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = ["Metric", "MetricsHook", "NullMetrics", "RecordingMetrics", "record_metric"]


class MetricsHook(Protocol):
    def counter(self, name: str, value: float = 1, **labels: str) -> None: ...

    def gauge(self, name: str, value: float, **labels: str) -> None: ...

    def histogram(self, name: str, value: float, **labels: str) -> None: ...


class NullMetrics:
    def counter(self, name: str, value: float = 1, **labels: str) -> None:
        pass

    def gauge(self, name: str, value: float, **labels: str) -> None:
        pass

    def histogram(self, name: str, value: float, **labels: str) -> None:
        pass


@dataclass(frozen=True, slots=True)
class Metric:
    kind: str
    name: str
    value: float
    labels: dict[str, str]


@dataclass(slots=True)
class RecordingMetrics:
    """Простой hook для тестов и небольших приложений; не ограничивает выбор backend'а."""

    events: list[Metric] = field(default_factory=list)

    def counter(self, name: str, value: float = 1, **labels: str) -> None:
        self.events.append(Metric("counter", name, float(value), labels))

    def gauge(self, name: str, value: float, **labels: str) -> None:
        self.events.append(Metric("gauge", name, float(value), labels))

    def histogram(self, name: str, value: float, **labels: str) -> None:
        self.events.append(Metric("histogram", name, float(value), labels))


def record_metric(hook: MetricsHook, kind: str, name: str, value: float = 1, **labels: str) -> None:
    """Сбой пользовательского backend'а метрик не должен ломать доменную операцию."""
    with contextlib.suppress(Exception):
        getattr(hook, kind)(name, value, **labels)


class MetricsReporter:
    def __init__(self, hub: IdentityHub, interval: float = 10.0) -> None:
        self.hub = hub
        self.interval = interval
        self._last: float | None = None

    async def run_due(self) -> int:
        now = self.hub.clock.monotonic()
        if self._last is not None and now - self._last < self.interval:
            return 0
        await self.collect()
        self._last = now
        return 0  # gauges не являются обработанными элементами очереди Dispatcher.drain()

    async def collect(self) -> None:
        snapshot = await self.hub.store.metrics_snapshot()
        hook = self.hub.metrics
        for consumer, lag in snapshot.cursor_lag.items():
            record_metric(hook, "gauge", "identity_hub_dispatcher_cursor_lag", lag, consumer=consumer)
        for queue, size in snapshot.delivery_queue.items():
            record_metric(hook, "gauge", "identity_hub_delivery_queue", size, queue=queue)
