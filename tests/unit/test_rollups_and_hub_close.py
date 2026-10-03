"""F3.10: rollups без квадратичного перечитывания, предел буфера метрик, закрытие хаба."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from identity_hub import IdentityHub, StaticSecretsProvider, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.events import EventDraft
from identity_hub.metrics import RollupMetricsBuffer
from identity_hub.rollups import BucketSize
from identity_hub.testing import MemoryStore, make_identity, make_service

pytestmark = pytest.mark.unit

START = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


class _Transport:
    def __init__(self) -> None:
        self.closed = 0

    async def send_message(
        self, bot_token: SecretStr, chat_id: Any, text: str, *, thread_id: Any = None
    ) -> None:
        return None

    async def aclose(self) -> None:
        self.closed += 1


class _Recorder:
    def __init__(self) -> None:
        self.counters: list[tuple[str, float, dict[str, str]]] = []

    def counter(self, name: str, value: float = 1, **labels: str) -> None:
        self.counters.append((name, value, labels))

    def gauge(self, name: str, value: float, **labels: str) -> None:
        return None

    def histogram(self, name: str, value: float, **labels: str) -> None:
        return None


async def _hub(**options: Any) -> tuple[IdentityHub, FakeClock, Any]:
    clock = FakeClock(START)
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[make_service("svc")],
        clock=clock,
        **options,
    )
    await hub.start()
    return hub, clock, hub.space("s", "S", create=True)


async def _emit(space: Any, count: int) -> None:
    for _ in range(count):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.registered", actor="t", service="svc"))


async def _total(hub: IdentityHub, size: BucketSize, metric: str = "identities.added") -> float:
    rows = await hub._rollups.rows(
        "s", metric=metric, bucket_size=size, start=START - timedelta(days=2), end=START + timedelta(days=2)
    )
    return sum(row.value for row in rows)


# ---------------------------------------------------------------- R2-F11


async def test_rollup_batches_do_not_reread_the_whole_day() -> None:
    """R1: число перечитанных событий растёт линейно, а не квадратично по суткам."""
    hub, clock, space = await _hub()
    reads: list[int] = []
    original = hub._store.rollup_source_events

    async def spy(slug: str, start: datetime, end: datetime) -> Any:
        rows = await original(slug, start, end)
        reads.append(len(rows))
        return rows

    hub._store.rollup_source_events = spy  # type: ignore[method-assign]
    for _ in range(6):  # шесть пачек, каждая в своей минуте
        await _emit(space, 50)
        await hub.dispatcher.drain()
        clock.advance(60)

    assert sum(reads) <= 300 * 2  # раньше: 50 + 100 + … + 300 = 1050 и дальше квадратично
    for size in BucketSize:
        assert await _total(hub, size) == 300  # минутные, часовые и суточные корзины согласованы


async def test_replayed_batch_and_late_events_keep_totals_exact() -> None:
    """Повтор пачки не удваивает значения; событие старше хранимых минут пересчитывается по журналу."""
    hub, clock, space = await _hub()
    await _emit(space, 5)
    await hub.dispatcher.drain()
    captured: list[Any] = []

    async def capture(events: list[Any]) -> None:
        captured.extend(events)

    await hub._store.process_events("capture", capture, limit=100, external_only=False)
    await hub._rollups.handle_events(captured)  # at-least-once: пачка пришла повторно
    assert [await _total(hub, size) for size in BucketSize] == [5, 5, 5]

    clock.advance(timedelta(days=3))  # минутные корзины той поры уже очищены
    await hub._rollups.purge_minutes()
    await hub._rollups.handle_events(captured)
    assert await _total(hub, BucketSize.HOUR) == 5 and await _total(hub, BucketSize.DAY) == 5


async def test_rebuild_keeps_telemetry_and_refuses_ranges_beyond_retention() -> None:
    hub, clock, space = await _hub()
    await make_identity(space, service="svc")
    pool = space.pools.define("p", service="svc")
    async with pool.lease(for_operation="work") as lease, lease.operation("work"):
        pass
    await hub.dispatcher.drain()
    telemetry = [key for key in hub._store._state.rollups if key.metric.startswith("telemetry.")]  # type: ignore[attr-defined]
    assert telemetry

    rebuilt = await hub._rollups.rebuild("s", START - timedelta(hours=1), START + timedelta(hours=1))

    assert rebuilt > 0
    after = [key for key in hub._store._state.rollups if key.metric.startswith("telemetry.")]  # type: ignore[attr-defined]
    assert sorted(after, key=repr) == sorted(telemetry, key=repr)  # телеметрию не восстановить из журнала

    old = clock.now() - hub.retention.events - timedelta(days=1)
    with pytest.raises(ValidationFailed):
        await hub._rollups.rebuild("s", old, old + timedelta(hours=1))
    assert await hub._rollups.rebuild("s", old, old + timedelta(hours=1), force=True) == 0


# ---------------------------------------------------------------- R2-F12


async def test_metric_buffer_is_bounded_without_a_dispatcher() -> None:
    """M1: процесс без диспетчера не копит буфер метрик бесконечно."""
    recorder = _Recorder()
    hub, clock, space = await _hub(metrics=recorder)
    await make_identity(space, service="svc")
    pool = space.pools.define("p", service="svc")
    buffer: RollupMetricsBuffer = hub._metric_rollups
    buffer.max_keys = 40

    for _ in range(120):
        async with pool.lease(for_operation="work") as lease, lease.operation("work"):
            pass
        clock.advance(60)

    assert len(buffer._pending) <= 40
    assert buffer.dropped > 0
    kept = {key[2] for key in buffer._pending}
    assert min(kept) > START + timedelta(minutes=60)  # отброшены самые старые минуты
    overflow = [
        (value, labels)
        for name, value, labels in recorder.counters
        if name == "identity_hub_failures_total" and labels["scope"] == "metrics_buffer"
    ]
    assert sum(value for value, _ in overflow) == buffer.dropped  # потеря видна во внешних метриках
    assert {labels["reason"] for _, labels in overflow} == {"overflow"}


async def test_metric_buffer_rejects_new_keys_when_one_minute_fills_it() -> None:
    """Вся ёмкость занята текущей минутой: уже принятые значения целы, новые не принимаются."""
    hub, _clock, _space = await _hub()
    buffer: RollupMetricsBuffer = hub._metric_rollups
    buffer.max_keys = 3

    for index in range(5):
        buffer.counter("identity_hub_custom_total", space="s", kind=f"k{index}")
    buffer.counter("identity_hub_custom_total", space="s", kind="k0")  # известный ключ копится дальше

    assert len(buffer._pending) == 3 and buffer.dropped == 2
    assert sorted(buffer._pending.values()) == [1.0, 1.0, 2.0]


async def test_flush_metrics_is_public_and_empties_the_buffer() -> None:
    hub, _clock, space = await _hub()
    await make_identity(space, service="svc")
    pool = space.pools.define("p", service="svc")
    async with pool.lease(for_operation="work") as lease, lease.operation("work"):
        pass
    assert hub._metric_rollups._pending

    assert await hub.flush_metrics() > 0

    assert not hub._metric_rollups._pending and not hub._metric_rollups._retries
    assert await hub.flush_metrics() == 0


# ---------------------------------------------------------------- R1-10


async def test_aclose_flushes_buffers_and_closes_owned_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from identity_hub import telegram_notify

    created: list[_Transport] = []

    def factory(**_kwargs: Any) -> _Transport:
        created.append(_Transport())
        return created[-1]

    monkeypatch.setattr(telegram_notify, "HttpTelegramTransport", factory)
    hub, _clock, space = await _hub()
    await make_identity(space, service="svc")
    pool = space.pools.define("p", service="svc")
    async with pool.lease(for_operation="work") as lease, lease.operation("work"):
        pass
    hub._record_lease_loss("s", "worker-1")

    await hub.aclose()
    await hub.aclose()  # повтор безопасен

    assert created[0].closed == 1
    assert not hub._metric_rollups._pending and not hub._lease_losses
    assert await _total(hub, BucketSize.MINUTE, "telemetry.operation_duration_seconds.count") >= 1


async def test_user_transport_is_not_closed_and_hub_is_a_context_manager() -> None:
    transport = _Transport()
    clock = FakeClock(START)
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        services=[make_service("svc")],
        clock=clock,
        telegram_transport=transport,
    )

    async with hub as entered:
        assert entered is hub
        await make_identity(hub.space("s", "S", create=True), service="svc")  # start() выполнен

    assert transport.closed == 0  # транспорт создал вызывающий — он его и закрывает


async def test_day_bucket_survives_purge_of_its_hour_buckets() -> None:
    """F4.1b: часовые корзины суток уже очищены — сутки пересчитываются по журналу, а не по остаткам."""
    hub, clock, space = await _hub()
    await _emit(space, 2)
    await hub.dispatcher.drain()
    clock.advance(3600)
    await _emit(space, 1)  # событие дождётся обработки позже срока хранения часовых корзин
    clock.advance(timedelta(days=100))
    counts = await hub._store.cleanup_maintenance(
        {"rollup_hours": hub.retention.rollup_hours}, frozenset({"rollup_hours"})
    )
    assert counts["rollup_hours"] > 0

    await hub.dispatcher.drain()

    assert await _total(hub, BucketSize.DAY) == 3
