"""Периодическое обслуживание Identity Hub (ТЗ §8.9)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

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
            result = await self._run_task(name, task)
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
        refs = await self.hub.store.maintenance_spaces()
        return [self.hub.space(ref.slug, ref.title, space_id=ref.id) for ref in refs]

    async def _normalize(self) -> MaintenanceReport:
        report = MaintenanceReport()
        for space in await self._spaces():
            async with space._tx(own=True) as tx:
                due = await tx.due_identity_timers(self.schedule.batch_size)
            for identity_id, region in due:
                decision = await space._expire(identity_id, region)
                report.add("region_timers", int(decision.changed))
            batch = self.schedule.batch_size
            report.add("verifications", await space.maintenance.expire_verifications(limit=batch))
            report.add("challenges", await space.maintenance.expire_challenges(limit=batch))
        report.add("lease_loss_aggregates", await self.hub.flush_lease_losses())
        return report

    async def _leases(self) -> MaintenanceReport:
        report = MaintenanceReport()
        for space in await self._spaces():
            async with space._tx(own=True) as tx:
                report.add("leases", await tx.expire_leases(self.schedule.batch_size))
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
        }

    async def _hourly(self) -> MaintenanceReport:
        counts = await self.hub.store.cleanup_maintenance(
            self._retention(), frozenset({"quota_windows", "operation_keys"})
        )
        return MaintenanceReport(Counter(counts))

    async def _daily(self) -> MaintenanceReport:
        counts = await self.hub.store.cleanup_maintenance(
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
                }
            ),
        )
        return MaintenanceReport(Counter(counts))
