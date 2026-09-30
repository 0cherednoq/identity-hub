"""Bounded, secret-free administrative analytics read models."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from identity_hub._space import SpaceBase
from identity_hub.api._base import legacy
from identity_hub.errors import ValidationFailed
from identity_hub.events import Event
from identity_hub.rollups import BucketSize, RollupRow, bucket_start, next_bucket
from identity_hub.snapshots import snapshot_hour

__all__ = [
    "MAX_GROUPS",
    "MAX_POINTS",
    "MAX_RANGE",
    "METRIC_DIMENSIONS",
    "AnalyticsPoint",
    "AnalyticsSeries",
    "AnalyticsSeriesGroup",
    "BanRateReport",
    "CohortCell",
    "CohortReport",
    "CohortRow",
    "FunnelReport",
    "FunnelStage",
    "HeatmapCell",
    "ProxyQualityReport",
    "RateItem",
    "TimeToReport",
    "UtilizationReport",
]

MAX_POINTS = 1_000
MAX_GROUPS = 50
MAX_RANGE = timedelta(days=366)

METRIC_DIMENSIONS: dict[str, frozenset[str]] = {
    "events.total": frozenset({"service", "type"}),
    "identities.added": frozenset({"service", "method"}),
    "auth.authenticated": frozenset({"service"}),
    "auth.session_lost": frozenset({"service"}),
    "logins.succeeded": frozenset({"service"}),
    "logins.failed": frozenset({"service", "code"}),
    "challenges.opened": frozenset({"service", "kind"}),
    "challenges.answered": frozenset({"service", "kind"}),
    "challenges.expired": frozenset({"service", "kind"}),
    "verifications.verified": frozenset({"service", "factor"}),
    "verifications.rejected": frozenset({"service", "factor", "code"}),
    "tiers.reached": frozenset({"service", "tier"}),
    "tiers.lost": frozenset({"service", "tier"}),
    "standings.entered": frozenset({"service", "state", "code"}),
    "standings.recovered": frozenset({"service", "state"}),
    "state.overrides": frozenset({"service", "region"}),
    "state.drift": frozenset({"service", "region"}),
    "availability.cooling": frozenset({"service"}),
    "quotas.exhausted": frozenset({"service", "kind"}),
    "lifecycle.retired": frozenset({"service"}),
    "leases.acquired": frozenset({"service", "pool"}),
    "leases.ended": frozenset({"service", "pool", "reason"}),
    "operations.succeeded": frozenset({"service", "kind"}),
    "operations.failed": frozenset({"service", "kind", "blame", "code"}),
    "proxies.status": frozenset({"pool", "state"}),
    "telemetry.lease_empty_total": frozenset({"service", "pool"}),
    "telemetry.lease_wait_seconds.sum": frozenset({"service", "pool", "outcome"}),
    "telemetry.lease_wait_seconds.count": frozenset({"service", "pool", "outcome"}),
    "telemetry.lease_duration_seconds.sum": frozenset({"service", "pool", "reason"}),
    "telemetry.lease_duration_seconds.count": frozenset({"service", "pool", "reason"}),
    "telemetry.operation_duration_seconds.sum": frozenset({"service", "operation", "status"}),
    "telemetry.operation_duration_seconds.count": frozenset({"service", "operation", "status"}),
}


@dataclass(frozen=True, slots=True)
class AnalyticsPoint:
    at: datetime
    value: float


@dataclass(frozen=True, slots=True)
class AnalyticsSeriesGroup:
    key: str
    points: tuple[AnalyticsPoint, ...]
    total: float
    previous_total: float


@dataclass(frozen=True, slots=True)
class AnalyticsSeries:
    metric: str
    bucket_size: BucketSize
    group_by: str | None
    groups: tuple[AnalyticsSeriesGroup, ...]
    coverage: Literal["complete", "partial"]
    cache_tag: str


@dataclass(frozen=True, slots=True)
class FunnelStage:
    key: str
    count: int
    share: float


@dataclass(frozen=True, slots=True)
class FunnelReport:
    stages: tuple[FunnelStage, ...]
    cache_tag: str


@dataclass(frozen=True, slots=True)
class TimeToReport:
    metric: str
    samples: int
    median_seconds: float | None
    p90_seconds: float | None
    average_attempts: float | None
    cache_tag: str


@dataclass(frozen=True, slots=True)
class CohortCell:
    offset_days: int
    eligible: int
    good: int
    share: float | None


@dataclass(frozen=True, slots=True)
class CohortRow:
    cohort_start: datetime
    size: int
    cells: tuple[CohortCell, ...]


@dataclass(frozen=True, slots=True)
class CohortReport:
    rows: tuple[CohortRow, ...]
    coverage: Literal["complete", "partial"]
    cache_tag: str


@dataclass(frozen=True, slots=True)
class RateItem:
    key: str
    added: int
    banned: int
    rate: float


@dataclass(frozen=True, slots=True)
class BanRateReport:
    group_by: str
    items: tuple[RateItem, ...]
    cache_tag: str


@dataclass(frozen=True, slots=True)
class HeatmapCell:
    weekday: int
    hour: int
    leases: int
    operations: int
    failed: int


@dataclass(frozen=True, slots=True)
class UtilizationReport:
    leased_seconds: float
    eligible_current: int
    utilization: float | None
    heatmap: tuple[HeatmapCell, ...]
    cache_tag: str


@dataclass(frozen=True, slots=True)
class ProxyQualityReport:
    failures: int
    quarantined: int
    dead: int
    recovered: int
    cache_tag: str


def _validate_range(start: datetime, end: datetime) -> None:
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValidationFailed("analytics range must be aware and non-empty")
    if end - start > MAX_RANGE:
        raise ValidationFailed("analytics range exceeds 366 days")


def _etag(value: object) -> str:
    payload = json.dumps(
        asdict(cast(Any, value)) if hasattr(value, "__dataclass_fields__") else value,
        default=str,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _row_dimensions(row: RollupRow) -> dict[str, str]:
    return {"service": row.key.service, **row.key.dimensions}


async def _snapshot_coverage(
    space: AnalyticsMixin, start: datetime, end: datetime
) -> Literal["complete", "partial"]:
    frames = await space.hub.state_snapshots.frames(space.slug, start=start, end=end)
    present = {frame.taken_at for frame in frames if frame.complete}
    cursor = snapshot_hour(start)
    last = snapshot_hour(end - timedelta(microseconds=1))
    while cursor <= last:
        if cursor not in present:
            return "partial"
        cursor += timedelta(hours=1)
    return "complete"


class AnalyticsMixin(SpaceBase):
    async def _analytics_series(
        self,
        metric: str,
        *,
        start: datetime,
        end: datetime,
        bucket_size: BucketSize,
        group_by: str | None = None,
        dimensions: Mapping[str, str] | None = None,
    ) -> AnalyticsSeries:
        _validate_range(start, end)
        if not isinstance(bucket_size, BucketSize):
            raise ValidationFailed("unsupported analytics bucket")
        allowed = METRIC_DIMENSIONS.get(metric)
        if allowed is None:
            raise ValidationFailed(f"unknown analytics metric: {metric}")
        requested = set(dimensions or ()) | ({group_by} if group_by else set())
        unknown = requested - allowed
        if unknown:
            raise ValidationFailed(f"unsupported dimensions for {metric}: {sorted(unknown)}")
        space = await self.ref()
        first = bucket_start(start, bucket_size, space.timezone)
        buckets: list[datetime] = []
        cursor = first
        while cursor < end:
            buckets.append(cursor)
            cursor = next_bucket(cursor, bucket_size, space.timezone)
        if len(buckets) > MAX_POINTS:
            raise ValidationFailed("analytics response exceeds 1000 points")
        duration = end - start
        previous_start, previous_end = first - duration, first
        current = await self.hub.store.read_rollups(
            self.slug,
            metric=metric,
            bucket_size=bucket_size,
            start=first,
            end=end,
            service=None,
            dims=None,
        )
        previous = await self.hub.store.read_rollups(
            self.slug,
            metric=metric,
            bucket_size=bucket_size,
            start=bucket_start(previous_start, bucket_size, space.timezone),
            end=previous_end,
            service=None,
            dims=None,
        )
        filters = dict(dimensions or {})

        def selected(row: RollupRow) -> bool:
            values = _row_dimensions(row)
            return all(values.get(key) == value for key, value in filters.items())

        grouped: defaultdict[str, defaultdict[datetime, float]] = defaultdict(lambda: defaultdict(float))
        previous_totals: defaultdict[str, float] = defaultdict(float)
        for row in current:
            if selected(row):
                values = _row_dimensions(row)
                grouped[values.get(group_by, "") if group_by else "all"][row.key.bucket_start] += row.value
        for row in previous:
            if selected(row):
                values = _row_dimensions(row)
                previous_totals[values.get(group_by, "") if group_by else "all"] += row.value
        keys = sorted(set(grouped) | set(previous_totals)) or ["all"]
        if len(keys) > MAX_GROUPS or len(keys) * len(buckets) > MAX_POINTS:
            raise ValidationFailed("analytics response exceeds group or point limit")
        groups = tuple(
            AnalyticsSeriesGroup(
                key,
                tuple(AnalyticsPoint(at, grouped[key][at]) for at in buckets),
                sum(grouped[key].values()),
                float(previous_totals[key]),
            )
            for key in keys
        )
        draft = AnalyticsSeries(metric, bucket_size, group_by, groups, "complete", "")
        return AnalyticsSeries(metric, bucket_size, group_by, groups, "complete", _etag(draft))

    async def _analytics_funnel(
        self, *, start: datetime, end: datetime, service: str | None = None
    ) -> FunnelReport:
        events = await self._analytics_events(start, max(end, self.hub.clock.now()), service=service)
        added_ids = {
            event.identity_id
            for event in events
            if event.type == "identity.registered" and event.identity_id and event.occurred_at < end
        }
        imported = sum(
            int(event.data.get("count", 0))
            for event in events
            if event.type == "identity.imported" and event.occurred_at < end
        )
        authenticated = {
            event.identity_id
            for event in events
            if event.type == "identity.auth.changed"
            and event.data.get("to") == "authenticated"
            and event.identity_id in added_ids
        }
        tier_names = sorted(
            {
                str(event.data.get("tier"))
                for event in events
                if event.type == "identity.tier.reached" and event.identity_id in added_ids
            }
        )
        denominator = len(added_ids) + imported
        pairs = [("added", denominator), ("authenticated", len(authenticated))]
        for tier in tier_names:
            identities = {
                event.identity_id
                for event in events
                if event.type == "identity.tier.reached"
                and str(event.data.get("tier")) == tier
                and event.identity_id in added_ids
            }
            pairs.append((f"tier:{tier}", len(identities)))
        stages = tuple(
            FunnelStage(key, count, count / denominator if denominator else 0.0) for key, count in pairs
        )
        return FunnelReport(stages, _etag([asdict(stage) for stage in stages]))

    async def _analytics_time_to(
        self, metric: str, *, start: datetime, end: datetime, service: str | None = None
    ) -> TimeToReport:
        events = await self._analytics_events(start, max(end, self.hub.clock.now()), service=service)
        starts: dict[object, datetime] = {}
        finishes: dict[object, datetime] = {}
        attempts: Counter[object] = Counter()
        if metric in {"added_to_authenticated", "added_to_banned"}:
            target_type = (
                "identity.auth.changed" if metric.endswith("authenticated") else "identity.standing.changed"
            )
            target_state = "authenticated" if metric.endswith("authenticated") else "banned"
            for event in events:
                if event.identity_id is None:
                    continue
                if event.type == "identity.registered" and event.occurred_at < end:
                    starts.setdefault(event.identity_id, event.occurred_at)
                elif event.type == target_type and event.data.get("to") == target_state:
                    finishes.setdefault(event.identity_id, event.occurred_at)
        elif metric.startswith("verification:"):
            factor = metric.split(":", 1)[1]
            for event in events:
                if event.type != "identity.verification.changed" or event.identity_id is None:
                    continue
                if event.data.get("key") != factor:
                    continue
                if event.occurred_at < end and event.data.get("to") in {"pending", "under_review"}:
                    starts.setdefault(event.identity_id, event.occurred_at)
                elif event.data.get("to") == "verified":
                    finishes.setdefault(event.identity_id, event.occurred_at)
                    raw_attempts = event.data.get("attempts")
                    if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool):
                        attempts[event.identity_id] = max(0, raw_attempts)
        else:
            raise ValidationFailed("unknown time-to metric")
        durations = [
            (finished - starts[identity]).total_seconds()
            for identity, finished in finishes.items()
            if identity in starts and finished >= starts[identity]
        ]
        average_attempts = (
            sum(attempts[identity] for identity in finishes if identity in starts) / len(durations)
            if durations and attempts
            else None
        )
        draft = TimeToReport(
            metric,
            len(durations),
            _percentile(durations, 0.5),
            _percentile(durations, 0.9),
            average_attempts,
            "",
        )
        return TimeToReport(
            draft.metric,
            draft.samples,
            draft.median_seconds,
            draft.p90_seconds,
            draft.average_attempts,
            _etag(draft),
        )

    async def _analytics_cohorts(
        self,
        *,
        start: datetime,
        end: datetime,
        offsets: Sequence[int] = (1, 7, 14, 30),
        service: str | None = None,
    ) -> CohortReport:
        events = await self._analytics_events(start, max(end, self.hub.clock.now()), service=service)
        if tuple(offsets) != tuple(sorted(set(offsets))) or any(day < 1 or day > 90 for day in offsets):
            raise ValidationFailed("cohort offsets must be unique ascending days in 1..90")
        space = await self.ref()
        zone = ZoneInfo(space.timezone)
        registered: dict[object, datetime] = {}
        transitions: defaultdict[object, list[tuple[datetime, str]]] = defaultdict(list)
        for event in events:
            if event.identity_id is None:
                continue
            if event.type == "identity.registered" and event.occurred_at < end:
                registered[event.identity_id] = event.occurred_at
            elif event.type == "identity.standing.changed":
                transitions[event.identity_id].append((event.occurred_at, str(event.data.get("to"))))
        cohorts: defaultdict[datetime, list[object]] = defaultdict(list)
        for identity, added_at in registered.items():
            local = added_at.astimezone(zone)
            monday = (local - timedelta(days=local.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            cohorts[monday.astimezone(UTC)].append(identity)
        rows = []
        now = self.hub.clock.now()
        for cohort_start, identities in sorted(cohorts.items()):
            cells = []
            for days in offsets:
                eligible_ids = [
                    identity for identity in identities if registered[identity] + timedelta(days=days) <= now
                ]
                good = 0
                for identity in eligible_ids:
                    cutoff = registered[identity] + timedelta(days=days)
                    state = "good"
                    for changed_at, target in sorted(transitions[identity]):
                        if changed_at > cutoff:
                            break
                        state = target
                    good += state == "good"
                cells.append(
                    CohortCell(
                        days, len(eligible_ids), good, good / len(eligible_ids) if eligible_ids else None
                    )
                )
            rows.append(CohortRow(cohort_start, len(identities), tuple(cells)))
        coverage = await _snapshot_coverage(self, start, end)
        return CohortReport(tuple(rows), coverage, _etag([asdict(row) for row in rows]))

    async def _analytics_ban_rate(
        self, *, start: datetime, end: datetime, group_by: str, service: str | None = None
    ) -> BanRateReport:
        if group_by not in {"service", "batch", "tag", "proxy_pool"}:
            raise ValidationFailed("ban-rate dimension must be service, batch, tag or proxy_pool")
        events = await self._analytics_events(start, max(end, self.hub.clock.now()), service=service)
        groups: defaultdict[UUID, set[str]] = defaultdict(set)
        banned: set[UUID] = set()
        for event in events:
            identity = event.identity_id
            if event.type == "identity.registered" and identity is not None and event.occurred_at < end:
                if group_by == "service":
                    groups[identity].add(event.service or "")
                elif group_by == "batch":
                    labels = event.data.get("labels", {})
                    if isinstance(labels, Mapping) and labels.get("batch") is not None:
                        groups[identity].add(str(labels["batch"]))
                elif group_by == "tag":
                    groups[identity].update(str(tag) for tag in event.data.get("tags", []))
            elif event.type == "proxy.binding.changed" and identity is not None and group_by == "proxy_pool":
                if event.data.get("pool") is not None:
                    groups[identity].add(str(event.data["pool"]))
            elif (
                event.type == "identity.standing.changed"
                and event.data.get("to") == "banned"
                and identity is not None
            ):
                banned.add(identity)
        added: Counter[str] = Counter()
        bans: Counter[str] = Counter()
        for grouped_identity, keys in groups.items():
            for key in keys:
                added[key] += 1
                bans[key] += grouped_identity in banned
        if len(added) > MAX_GROUPS:
            raise ValidationFailed("ban-rate response exceeds 50 groups")
        items = tuple(RateItem(key, added[key], bans[key], bans[key] / added[key]) for key in sorted(added))
        return BanRateReport(group_by, items, _etag([asdict(item) for item in items]))

    async def _analytics_utilization(
        self, *, start: datetime, end: datetime, service: str | None = None
    ) -> UtilizationReport:
        _validate_range(start, end)
        async with self._tx(own=True) as tx:
            rows = await tx.lease_read_rows(start)
            overview = await tx.overview_snapshot()
        rows = [
            row
            for row in rows
            if row.acquired_at < end
            and (row.released_at is None or row.released_at > start)
            and (service is None or row.service == service)
        ]
        leased_seconds = sum(
            max(0.0, (min(row.released_at or end, end) - max(row.acquired_at, start)).total_seconds())
            for row in rows
        )
        eligible = sum(
            item.eligible for item in overview.services if service is None or item.service == service
        )
        denominator = eligible * (end - start).total_seconds()
        cells: defaultdict[tuple[int, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        zone = ZoneInfo((await self.ref()).timezone)
        for row in rows:
            local = row.acquired_at.astimezone(zone)
            cell = cells[local.weekday(), local.hour]
            cell[0] += 1
            cell[1] += row.succeeded + row.failed
            cell[2] += row.failed
        heatmap = tuple(HeatmapCell(day, hour, *cells[day, hour]) for day, hour in sorted(cells))
        draft = UtilizationReport(
            leased_seconds,
            eligible,
            min(1.0, leased_seconds / denominator) if denominator else None,
            heatmap,
            "",
        )
        return UtilizationReport(
            draft.leased_seconds,
            draft.eligible_current,
            draft.utilization,
            draft.heatmap,
            _etag(draft),
        )

    async def _analytics_proxy_quality(self, *, start: datetime, end: datetime) -> ProxyQualityReport:
        events = await self._analytics_events(start, end)
        failures = sum(
            event.type == "operation.failed" and event.data.get("blame") == "proxy" for event in events
        )
        targets = Counter(
            str(event.data.get("to")) for event in events if event.type == "proxy.status.changed"
        )
        draft = ProxyQualityReport(
            failures,
            targets["quarantined"],
            targets["dead"],
            targets["alive"],
            "",
        )
        return ProxyQualityReport(
            draft.failures,
            draft.quarantined,
            draft.dead,
            draft.recovered,
            _etag(draft),
        )

    async def _analytics_events(
        self, start: datetime, end: datetime, *, service: str | None = None
    ) -> list[Event]:
        _validate_range(start, end)
        events = await self.hub.store.rollup_source_events(self.slug, start, end)
        return [event for event in events if service is None or event.service == service]

    analytics_ban_rate = legacy(_analytics_ban_rate, "analytics_ban_rate")
    analytics_cohorts = legacy(_analytics_cohorts, "analytics_cohorts")
    analytics_funnel = legacy(_analytics_funnel, "analytics_funnel")
    analytics_proxy_quality = legacy(_analytics_proxy_quality, "analytics_proxy_quality")
    analytics_series = legacy(_analytics_series, "analytics_series")
    analytics_time_to = legacy(_analytics_time_to, "analytics_time_to")
    analytics_utilization = legacy(_analytics_utilization, "analytics_utilization")
