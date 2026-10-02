"""Health-classified diagnostics for dispatcher, delivery and maintenance workers."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

Health = Literal["healthy", "delayed", "retrying", "stopped"]


@dataclass(frozen=True, slots=True)
class BackgroundProcess:
    name: str
    kind: str
    status: Health
    heartbeat_available: bool
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    lag_events: int | None
    lag_seconds: float | None
    backlog: int | None
    failed: int | None
    next_attempt_at: datetime | None
    last_error: str | None
    runbook_hint: str


@dataclass(frozen=True, slots=True)
class BackgroundSnapshot:
    generated_at: datetime
    processes: tuple[BackgroundProcess, ...]


def classify_health(
    *,
    heartbeat_at: datetime | None,
    now: datetime,
    delayed_after: float,
    stopped_after: float,
    lag: int = 0,
    lag_threshold: int = 100,
    retrying: bool = False,
) -> Health:
    if retrying:
        return "retrying"
    if heartbeat_at is None:
        return "stopped"
    age = max(0.0, (now - heartbeat_at).total_seconds())
    if age > stopped_after:
        return "stopped"
    if age > delayed_after or lag > lag_threshold:
        return "delayed"
    return "healthy"


async def background_snapshot(
    hub: IdentityHub,
    allowed_spaces: Collection[str] | None = None,
) -> BackgroundSnapshot:
    now = hub.clock.now()
    slugs = None if allowed_spaces is None or "*" in allowed_spaces else frozenset(allowed_spaces)
    metrics = await hub._store.metrics_snapshot(slugs)
    runtime = hub.dispatcher.runtime
    processes: list[BackgroundProcess] = []
    for consumer in sorted(hub.dispatcher.consumers):
        state = runtime[consumer]
        lag = metrics.cursor_lag.get(consumer, 0)
        heartbeat = state.last_attempt_at
        processes.append(
            BackgroundProcess(
                name=consumer,
                kind="dispatcher",
                status=classify_health(
                    heartbeat_at=heartbeat,
                    now=now,
                    delayed_after=10,
                    stopped_after=60,
                    lag=lag,
                    retrying=state.retrying,
                ),
                heartbeat_available=heartbeat is not None,
                last_attempt_at=heartbeat,
                last_success_at=state.last_success_at,
                lag_events=lag,
                lag_seconds=(
                    max(0.0, (now - updated).total_seconds())
                    if (updated := metrics.cursor_updated_at.get(consumer)) is not None and lag > 0
                    else 0.0
                    if lag == 0
                    else None
                ),
                backlog=metrics.delivery_queue.get(consumer),
                failed=metrics.delivery_failed.get(consumer),
                next_attempt_at=metrics.delivery_next_attempt_at.get(consumer),
                last_error=("Ошибка скрыта" if metrics.delivery_has_error.get(consumer) else None),
                runbook_hint="Проверьте процесс dispatcher, соединение с БД и журнал событий.",
            )
        )
    intervals = {
        "normalize": hub.maintenance.schedule.normalize_seconds,
        "leases": hub.maintenance.schedule.leases_seconds,
        "hourly": hub.maintenance.schedule.cleanup_hourly_seconds,
        "daily": hub.maintenance.schedule.cleanup_daily_seconds,
    }
    for name, interval in intervals.items():
        heartbeat = hub.maintenance.last_success_at.get(name)
        processes.append(
            BackgroundProcess(
                name=f"maintenance:{name}",
                kind="maintenance",
                status=classify_health(
                    heartbeat_at=heartbeat,
                    now=now,
                    delayed_after=interval * 2,
                    stopped_after=interval * 5,
                    retrying=name in hub.maintenance.retrying,
                ),
                heartbeat_available=heartbeat is not None,
                last_attempt_at=heartbeat,
                last_success_at=heartbeat,
                lag_events=None,
                lag_seconds=None,
                backlog=None,
                failed=None,
                next_attempt_at=None,
                last_error="Ошибка скрыта" if name in hub.maintenance.retrying else None,
                runbook_hint="Запустите maintenance --once и проверьте расписание процесса.",
            )
        )
    processes.append(
        BackgroundProcess(
            name="stale-probes",
            kind="probes",
            status="stopped",
            heartbeat_available=False,
            last_attempt_at=None,
            last_success_at=None,
            lag_events=None,
            lag_seconds=None,
            backlog=None,
            failed=None,
            next_attempt_at=None,
            last_error=None,
            runbook_hint="Heartbeat stale probes не хранится; настройте внешний supervisor.",
        )
    )
    return BackgroundSnapshot(now, tuple(processes))
