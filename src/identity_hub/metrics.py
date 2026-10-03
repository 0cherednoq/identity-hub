"""Независимый от Prometheus хук метрик (ТЗ §10.2)."""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from identity_hub.rollups import BucketSize, RollupDelta, RollupKey, RollupRow, bucket_start

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = [
    "CompositeMetrics",
    "Metric",
    "MetricsHook",
    "NullMetrics",
    "RecordingMetrics",
    "RollupMetricsBuffer",
    "record_metric",
]


_BUILTIN_LABELS: dict[str, frozenset[str]] = {
    "identity_hub_delivery_queue": frozenset({"queue"}),
    "identity_hub_dispatcher_cursor_lag": frozenset({"consumer"}),
    "identity_hub_failures_total": frozenset({"space", "service", "scope", "reason"}),
    "identity_hub_lease_acquisition_seconds": frozenset({"space", "service", "pool", "status"}),
    "identity_hub_lease_duration_seconds": frozenset({"space", "service", "pool", "reason"}),
    "identity_hub_lease_empty_total": frozenset({"space", "service", "pool"}),
    "identity_hub_lease_wait_seconds": frozenset({"space", "service", "pool", "outcome"}),
    "identity_hub_operation_duration_seconds": frozenset({"space", "service", "operation", "status"}),
    "identity_hub_pool_available": frozenset({"space", "service", "pool"}),
}
_FORBIDDEN_LABELS = frozenset({"identity_id", "lease_id", "event_id", "run_id", "external_ref"})


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


@dataclass(slots=True)
class CompositeMetrics:
    rollups: RollupMetricsBuffer
    downstream: MetricsHook

    def counter(self, name: str, value: float = 1, **labels: str) -> None:
        self.rollups.counter(name, value, **labels)
        self.downstream.counter(name, value, **labels)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        self.rollups.gauge(name, value, **labels)
        self.downstream.gauge(name, value, **labels)

    def histogram(self, name: str, value: float, **labels: str) -> None:
        self.rollups.histogram(name, value, **labels)
        self.downstream.histogram(name, value, **labels)


logger = logging.getLogger("identity_hub")

_BufferKey = tuple[str, str, datetime, str, tuple[tuple[str, str], ...]]


class RollupMetricsBuffer:
    """Non-blocking MetricsHook adapter flushed in exactly-once batches by dispatcher."""

    def __init__(self, hub: IdentityHub, *, max_keys: int = 10_000, max_retries: int = 10) -> None:
        self.hub = hub
        self._pending: defaultdict[_BufferKey, float] = defaultdict(float)
        self._retries: list[tuple[UUID, dict[_BufferKey, float]]] = []
        # Процесс без диспетчера буфер не сбрасывает: без предела он рос бы без конца (R2-F12).
        self.max_keys = max_keys
        self.max_retries = max_retries
        self.dropped = 0  # сколько ключей отброшено из-за предела

    def counter(self, name: str, value: float = 1, **labels: str) -> None:
        self._record("add", f"telemetry.{name.removeprefix('identity_hub_')}", value, labels)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        self._record("set", f"telemetry.{name.removeprefix('identity_hub_')}", value, labels)

    def histogram(self, name: str, value: float, **labels: str) -> None:
        base = f"telemetry.{name.removeprefix('identity_hub_')}"
        self._record("add", f"{base}.sum", value, labels)
        self._record("add", f"{base}.count", 1, labels)

    def _record(self, mode: str, metric: str, value: float, labels: dict[str, str]) -> None:
        space = labels.get("space")
        if space is None:
            return
        dimensions = tuple(sorted((key, item) for key, item in labels.items() if key not in {"space"}))
        minute = bucket_start(self.hub.clock.now(), BucketSize.MINUTE, "UTC")
        key = (mode, metric, minute, space, dimensions)
        if key not in self._pending and len(self._pending) >= self.max_keys and not self._make_room(minute):
            self._overflow(space, 1)  # вся ёмкость занята текущей минутой: новое значение не принято
            return
        if mode == "set":
            self._pending[key] = float(value)
        else:
            self._pending[key] += float(value)

    def _make_room(self, minute: datetime) -> bool:
        """Отбросить самую старую минуту целиком; `False`, если старее текущей минуты ничего нет."""
        oldest = min(key[2] for key in self._pending)
        if oldest >= minute:
            return False
        stale = [key for key in self._pending if key[2] == oldest]
        for key in stale:
            del self._pending[key]
        self._overflow(stale[0][3], len(stale))
        return True

    def _overflow(self, space: str, count: int) -> None:
        if not self.dropped:
            logger.warning(
                "буфер метрик переполнен (%d ключей): старые значения отбрасываются; "
                "вызывайте hub.flush_metrics() или запустите dispatcher",
                self.max_keys,
            )
        self.dropped += count
        # напрямую во внешний хук: запись через hub.metrics вернулась бы в этот же буфер
        downstream: MetricsHook | None = getattr(self.hub.metrics, "downstream", None)
        if downstream is None:
            return
        record_metric(
            downstream,
            "counter",
            "identity_hub_failures_total",
            value=count,
            space=space,
            service="",
            scope="metrics_buffer",
            reason="overflow",
        )

    async def flush(self) -> int:
        if self._pending:
            # минуты копятся по часам процесса; в пачку они попадают сдвинутыми на разницу с
            # часами хранилища — как и корзины событий, считаемые по времени базы (R2-D20)
            skew = await self.hub._store.current_time() - self.hub.clock.now()
            # время записи внутри минуты неизвестно: сдвиг — разница часов, округлённая до минут,
            # чтобы обычное расхождение в доли секунды не переносило значения в соседнюю минуту
            offset = timedelta(minutes=round(skew / timedelta(minutes=1)))
            self._retries.append((uuid4(), _shift_minutes(self._pending, offset)))
            self._pending.clear()
            while len(self._retries) > self.max_retries:  # хранилище давно недоступно
                self.dropped += len(self._retries.pop(0)[1])
        if not self._retries:
            return 0
        spaces = {space.slug: space for space in await self.hub._store.list_spaces()}
        batch_id, batch = self._retries[0]
        deltas: list[RollupDelta] = []
        for (mode, metric, minute, slug, labels), value in batch.items():
            space = spaces.get(slug)
            if space is None:
                continue
            label_map = dict(labels)
            service = label_map.pop("service", "")
            key = RollupKey(
                space.id,
                service,
                metric,
                tuple(sorted(label_map.items())),
                minute,
                BucketSize.MINUTE,
            )
            deltas.append(RollupDelta(RollupRow(key, value), additive=mode == "add"))
        await self.hub._store.apply_metric_rollup_batch(batch_id, deltas)
        self._retries.pop(0)
        return len(deltas)


def _shift_minutes(pending: Mapping[_BufferKey, float], offset: timedelta) -> dict[_BufferKey, float]:
    """Перенести минуты ключей на `offset`; совпавшие ключи складываются (`add`) или заменяются (`set`)."""
    shifted: defaultdict[_BufferKey, float] = defaultdict(float)
    for (mode, metric, minute, space, labels), value in pending.items():
        key = (mode, metric, bucket_start(minute + offset, BucketSize.MINUTE, "UTC"), space, labels)
        shifted[key] = value if mode == "set" else shifted[key] + value
    return dict(shifted)


def record_metric(hook: MetricsHook, kind: str, name: str, value: float = 1, **labels: str) -> None:
    """Сбой пользовательского backend'а метрик не должен ломать доменную операцию."""
    with contextlib.suppress(Exception):
        _validate_labels(name, labels)
        getattr(hook, kind)(name, value, **labels)


def _validate_labels(name: str, labels: dict[str, str]) -> None:
    if _FORBIDDEN_LABELS & labels.keys():
        raise ValueError("high-cardinality identity labels are forbidden")
    expected = _BUILTIN_LABELS.get(name)
    if expected is not None and labels.keys() != expected:
        raise ValueError(f"metric {name} labels must be {sorted(expected)}")
    if any(len(key) > 64 or len(value) > 128 for key, value in labels.items()):
        raise ValueError("metric labels are too long")


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
        snapshot = await self.hub._store.metrics_snapshot()
        hook = self.hub.metrics
        for consumer, lag in snapshot.cursor_lag.items():
            record_metric(hook, "gauge", "identity_hub_dispatcher_cursor_lag", lag, consumer=consumer)
        for queue, size in snapshot.delivery_queue.items():
            record_metric(hook, "gauge", "identity_hub_delivery_queue", size, queue=queue)
