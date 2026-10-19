"""Периодическое обслуживание Identity Hub (ТЗ §8.9)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub._space import logger
from identity_hub.errors import describe_error
from identity_hub.metrics import record_metric

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub, Retention, SpaceHandle

__all__ = ["Maintenance", "MaintenanceReport", "MaintenanceSchedule"]


@dataclass(frozen=True, slots=True)
class MaintenanceSchedule:
    normalize_seconds: float = 60
    leases_seconds: float = 60
    cleanup_hourly_seconds: float = 3600
    cleanup_daily_seconds: float = 86400
    batch_size: int = 500
    cleanup_batch_size: int = 1000  # строк на одну транзакцию удаления


@dataclass(slots=True)
class MaintenanceReport:
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def add(self, name: str, count: int) -> None:
        self.counts[name] += count

    def merge(self, counts: dict[str, int]) -> None:
        self.counts.update(counts)


class Maintenance:
    def __init__(self, hub: IdentityHub, schedule: MaintenanceSchedule | None = None) -> None:
        self.hub = hub
        self.schedule = schedule or MaintenanceSchedule()
        self._last: dict[str, float] = {}
        self.last_success_at: dict[str, datetime] = {}
        self.retrying: set[str] = set()

    async def _run_task(
        self, name: str, task: Callable[[], Awaitable[MaintenanceReport]]
    ) -> MaintenanceReport:
        try:
            report = await task()
        except Exception:
            self.retrying.add(name)
            raise
        self.retrying.discard(name)
        self.last_success_at[name] = self.hub.clock.now()
        return report

    async def run_due(self) -> int:
        """Запустить только задачи, чей монотонный интервал наступил; используется диспетчером."""
        now = self.hub.clock.monotonic()
        report = MaintenanceReport()
        groups = (
            ("normalize", self.schedule.normalize_seconds, self._normalize),
            ("leases", self.schedule.leases_seconds, self._leases),
            ("hourly", self.schedule.cleanup_hourly_seconds, self._hourly),
            ("daily", self.schedule.cleanup_daily_seconds, self._daily),
        )
        for name, interval, task in groups:
            if name in self._last and now - self._last[name] < interval:
                continue
            try:
                result = await self._run_task(name, task)
            except Exception as exc:
                # сбой одной группы не останавливает остальные; она повторится на следующем тике
                logger.error("задача обслуживания %s не выполнена: %s", name, describe_error(exc))
                self._failure("-", "-", name)
                continue
            report.counts.update(result.counts)
            self._last[name] = now
        return report.total

    async def run_all(self) -> MaintenanceReport:
        """Принудительно выполнить все задачи один раз (`identity-hub maintenance --once`)."""
        report = MaintenanceReport()
        for name, task in (
            ("normalize", self._normalize),
            ("leases", self._leases),
            ("hourly", self._hourly),
            ("daily", self._daily),
        ):
            current = await self._run_task(name, task)
            report.counts.update(current.counts)
        return report

    async def _spaces(self) -> list[SpaceHandle]:
        refs = await self.hub._store.maintenance_spaces()
        return [self.hub.space(ref.slug, ref.title, space_id=ref.id) for ref in refs]

    async def _normalize(self) -> MaintenanceReport:
        report = MaintenanceReport()
        for space in await self._spaces():
            async with space._tx(own=True) as tx:
                due = await tx.due_identity_timers(self.schedule.batch_size)
            for identity_id, region in due:
                try:
                    decision = await space._expire(identity_id, region)
                except Exception as exc:
                    # один аккаунт не останавливает таймеры остальных (R2-F06)
                    await self._skip(space, identity_id, f"таймер {region}", exc)
                    continue
                report.add("region_timers", int(decision.changed))
            batch = self.schedule.batch_size
            for name, expire in (
                ("verifications", space.maintenance.expire_verifications),
                ("challenges", space.maintenance.expire_challenges),
                ("logins", space.maintenance.expire_logins),
            ):
                try:
                    report.add(name, await expire(limit=batch))
                except Exception as exc:
                    logger.error(
                        "обслуживание %s пространства %s не выполнено: %s",
                        name,
                        space.slug,
                        describe_error(exc),
                    )
                    self._failure(space.slug, "-", name)
        report.add("lease_loss_aggregates", await self.hub.flush_lease_losses())
        return report

    async def _skip(self, space: SpaceHandle, identity_id: UUID, what: str, exc: Exception) -> None:
        """Пропустить аккаунт в обслуживании: предупреждение и метрика."""
        service = "-"
        reason = "failed"
        try:
            async with space._tx(own=True) as tx:
                row = await tx.get_identity(identity_id)
            if row is not None:
                service = row.service
                if all(row.service != known.name for known in self.hub.services):
                    reason = "unknown_service"  # сервис убран из кода, аккаунт остался в БД
        except Exception:
            pass
        logger.warning(
            "обслуживание: %s аккаунта %s пропущен (%s): %s", what, identity_id, reason, describe_error(exc)
        )
        self._failure(space.slug, service, reason)

    def _failure(self, space: str, service: str, reason: str) -> None:
        record_metric(
            self.hub.metrics,
            "counter",
            "identity_hub_failures_total",
            space=space,
            service=service,
            scope="maintenance",
            reason=reason,
        )

    async def _leases(self) -> MaintenanceReport:
        report = MaintenanceReport()
        for space in await self._spaces():
            async with space._tx(own=True) as tx:
                report.add("leases", await tx.expire_leases(self.schedule.batch_size))
        # операции провайдера с истёкшим claim — без сетевого I/O (IMP-11, D-223)
        report.add("provider_operations", await self.hub._provider_worker.run_once(self.schedule.batch_size))
        report.add("acceptance", await self.hub.workers.acceptance.run_once(self.schedule.batch_size))
        return report

    def _retention(self) -> dict[str, timedelta]:
        retention: Retention = self.hub.retention
        return {
            "events": retention.events,
            "lease_history": retention.lease_history,
            "lease_logs": retention.lease_logs,
            "deliveries": retention.deliveries,
            "operation_keys": retention.operation_keys,
            "import_runs": retention.import_runs,
            "provider_operations": retention.provider_operations,
            "metric_batches": retention.metric_batches,
            "closed_challenges": retention.closed_challenges,
            "rollup_hours": retention.rollup_hours,
            "rollup_days": retention.rollup_days,
        }

    async def _hourly(self) -> MaintenanceReport:
        counts = await self.hub._store.cleanup_maintenance(
            self._retention(),
            frozenset({"quota_windows", "operation_keys"}),
            batch_size=self.schedule.cleanup_batch_size,
        )
        return MaintenanceReport(Counter(counts))

    async def _daily(self) -> MaintenanceReport:
        counts = await self.hub._store.cleanup_maintenance(
            self._retention(),
            frozenset(
                {
                    "lease_logs",
                    "lease_history",
                    "webhook_deliveries",
                    "notifications",
                    "events",
                    "import_runs",
                    "provider_operations",
                    "metric_batches",
                    "closed_challenges",
                    "rollup_hours",
                    "rollup_days",
                }
            ),
            batch_size=self.schedule.cleanup_batch_size,
        )
        for name, count in counts.items():
            if name.startswith("events_held_by:"):
                record_metric(
                    self.hub.metrics,
                    "gauge",
                    "identity_hub_retention_held_events",
                    count,
                    consumer=name.removeprefix("events_held_by:"),
                )
        return MaintenanceReport(Counter(counts))
