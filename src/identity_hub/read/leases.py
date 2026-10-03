"""Operational, secret-free lease dashboard read models."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.errors import ValidationFailed
from identity_hub.store.base import LeaseReadRow

LeaseGroup = Literal["pool", "service", "holder"]


@dataclass(frozen=True, slots=True)
class LeaseFilters:
    pool: str | None = None
    service: str | None = None
    holder: str | None = None


@dataclass(frozen=True, slots=True)
class LeaseWindowMetrics:
    window: str
    window_seconds: int
    acquired: int
    completed: int
    unique_identities: int
    average_seconds: float | None
    p95_seconds: float | None
    leased_seconds: float
    utilization: float | None
    eligible_identities: int
    end_reasons: dict[str, int]
    operations: dict[str, int]
    succeeded: int
    failed: int
    failure_rate: float | None
    empty_picks: int | None = None
    wait_p95_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class LeaseSlice:
    value: str
    metrics: LeaseWindowMetrics
    loss_above_average: bool


@dataclass(frozen=True, slots=True)
class LeaseChartBucket:
    start: datetime
    end: datetime
    acquired: int
    lost_or_expired: int
    empty_picks: int | None = None


@dataclass(frozen=True, slots=True)
class LiveLease:
    lease_id: UUID
    identity_id: UUID
    service: str
    external_ref: str
    pool: str
    holder: str
    proxy_id: UUID | None
    acquired_at: datetime
    last_heartbeat_at: datetime
    heartbeat_lag_seconds: float
    heartbeat_stale: bool
    expires_at: datetime | None
    operations: dict[str, int]
    succeeded: int
    failed: int
    expired_unclosed: bool


@dataclass(frozen=True, slots=True)
class LeaseDashboard:
    generated_at: datetime
    filters: LeaseFilters
    group_by: LeaseGroup
    windows: tuple[LeaseWindowMetrics, ...]
    slices: tuple[LeaseSlice, ...]
    chart: tuple[LeaseChartBucket, ...]
    live: tuple[LiveLease, ...]
    expired_unclosed: tuple[LiveLease, ...]
    empty_pick_metrics_available: bool = False
    operation_timestamps_available: bool = False


@dataclass(frozen=True, slots=True)
class LeaseHistoryPage:
    items: tuple[LeaseReadRow, ...]
    next_before: tuple[datetime, UUID] | None


def _matches(row: LeaseReadRow, filters: LeaseFilters) -> bool:
    return not (
        (filters.pool is not None and row.pool != filters.pool)
        or (filters.service is not None and row.service != filters.service)
        or (filters.holder is not None and row.holder != filters.holder)
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _window_metrics(
    rows: list[LeaseReadRow],
    *,
    now: datetime,
    label: str,
    duration: timedelta,
    eligible: int,
) -> LeaseWindowMetrics:
    start = now - duration
    acquired = [row for row in rows if start <= row.acquired_at <= now]
    completed = [row for row in rows if row.released_at is not None and start <= row.released_at <= now]
    durations = [row.duration.total_seconds() for row in completed if row.duration is not None]
    leased_seconds = sum(
        max(
            0.0,
            (min(row.released_at or now, now) - max(row.acquired_at, start)).total_seconds(),
        )
        for row in rows
        if row.acquired_at < now and (row.released_at is None or row.released_at > start)
    )
    operations: Counter[str] = Counter()
    succeeded = 0
    failed = 0
    for row in rows:
        if row.acquired_at < now and (row.released_at is None or row.released_at > start):
            operations.update(row.operations)
            succeeded += row.succeeded
            failed += row.failed
    end_reasons = Counter(row.end_reason for row in completed if row.end_reason is not None)
    operation_count = succeeded + failed
    denominator = eligible * duration.total_seconds()
    return LeaseWindowMetrics(
        window=label,
        window_seconds=int(duration.total_seconds()),
        acquired=len(acquired),
        completed=len(completed),
        unique_identities=len({row.identity_id for row in acquired}),
        average_seconds=sum(durations) / len(durations) if durations else None,
        p95_seconds=_percentile(durations, 0.95),
        leased_seconds=leased_seconds,
        utilization=min(1.0, leased_seconds / denominator) if denominator > 0 else None,
        eligible_identities=eligible,
        end_reasons=dict(sorted(end_reasons.items())),
        operations=dict(sorted(operations.items())),
        succeeded=succeeded,
        failed=failed,
        failure_rate=failed / operation_count if operation_count else None,
    )


def _live(row: LeaseReadRow, now: datetime) -> LiveLease:
    lag = max(0.0, (now - row.last_heartbeat_at).total_seconds())
    ttl = (
        max(0.0, (row.expires_at - row.last_heartbeat_at).total_seconds())
        if row.expires_at is not None
        else None
    )
    expired = row.expires_at is not None and row.expires_at <= now
    return LiveLease(
        lease_id=row.lease_id,
        identity_id=row.identity_id,
        service=row.service,
        external_ref=row.external_ref,
        pool=row.pool,
        holder=row.holder,
        proxy_id=row.proxy_id,
        acquired_at=row.acquired_at,
        last_heartbeat_at=row.last_heartbeat_at,
        heartbeat_lag_seconds=lag,
        heartbeat_stale=expired or (ttl is not None and lag > ttl / 3),
        expires_at=row.expires_at,
        operations=dict(row.operations),
        succeeded=row.succeeded,
        failed=row.failed,
        expired_unclosed=expired,
    )


class LeasesMixin(SpaceBase):
    async def _lease_dashboard(
        self,
        *,
        filters: LeaseFilters | None = None,
        group_by: LeaseGroup = "pool",
    ) -> LeaseDashboard:
        selected = filters or LeaseFilters()
        if group_by not in ("pool", "service", "holder"):
            raise ValidationFailed("group_by: ожидается pool, service или holder")
        async with self._tx(own=True) as tx:
            now = await tx.now()
            rows = [
                row for row in await tx.lease_read_rows(now - timedelta(hours=24)) if _matches(row, selected)
            ]
            snapshot = await tx.overview_snapshot()
        eligible = sum(
            item.eligible
            for item in snapshot.services
            if selected.service is None or item.service == selected.service
        )
        windows = tuple(
            _window_metrics(rows, now=now, label=label, duration=duration, eligible=eligible)
            for label, duration in (
                ("15m", timedelta(minutes=15)),
                ("1h", timedelta(hours=1)),
                ("6h", timedelta(hours=6)),
                ("24h", timedelta(hours=24)),
            )
        )
        grouped: dict[str, list[LeaseReadRow]] = defaultdict(list)
        for row in rows:
            grouped[str(getattr(row, group_by))].append(row)
        overall_completed = windows[-1].completed
        overall_lost = sum(windows[-1].end_reasons.get(reason, 0) for reason in ("lost", "expired"))
        overall_loss_rate = overall_lost / overall_completed if overall_completed else 0.0
        slices = []
        for value, group_rows in sorted(grouped.items()):
            group_eligible = (
                next(
                    (item.eligible for item in snapshot.services if item.service == value),
                    eligible,
                )
                if group_by == "service"
                else eligible
            )
            metrics = _window_metrics(
                group_rows,
                now=now,
                label="24h",
                duration=timedelta(hours=24),
                eligible=group_eligible,
            )
            lost = sum(metrics.end_reasons.get(reason, 0) for reason in ("lost", "expired"))
            loss_rate = lost / metrics.completed if metrics.completed else 0.0
            slices.append(LeaseSlice(value, metrics, loss_rate > overall_loss_rate))
        day_start = now - timedelta(hours=24)
        bucket_start = day_start.replace(minute=0, second=0, microsecond=0, tzinfo=UTC)
        chart = []
        while bucket_start < now:
            bucket_end = bucket_start + timedelta(hours=1)
            chart.append(
                LeaseChartBucket(
                    bucket_start,
                    bucket_end,
                    sum(bucket_start <= row.acquired_at < bucket_end for row in rows),
                    sum(
                        row.end_reason in ("lost", "expired")
                        and row.released_at is not None
                        and bucket_start <= row.released_at < bucket_end
                        for row in rows
                    ),
                )
            )
            bucket_start = bucket_end
        live = tuple(_live(row, now) for row in rows if row.released_at is None)
        return LeaseDashboard(
            generated_at=now,
            filters=selected,
            group_by=group_by,
            windows=windows,
            slices=tuple(slices),
            chart=tuple(chart),
            live=live,
            expired_unclosed=tuple(item for item in live if item.expired_unclosed),
        )

    async def _lease_history_page(
        self,
        *,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
        filters: LeaseFilters | None = None,
    ) -> LeaseHistoryPage:
        if not 1 <= limit <= 200:
            raise ValidationFailed("limit: от 1 до 200")
        selected = filters or LeaseFilters()
        async with self._tx(own=True) as tx:
            rows = await tx.lease_read_page(
                limit=limit + 1,
                before=before,
                pool=selected.pool,
                service=selected.service,
                holder=selected.holder,
            )
        items = tuple(rows[:limit])
        next_before = (items[-1].acquired_at, items[-1].lease_id) if len(rows) > limit and items else None
        return LeaseHistoryPage(items, next_before)
