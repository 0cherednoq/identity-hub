"""Idempotent event time rollups for administrative analytics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from identity_hub.errors import ValidationFailed
from identity_hub.events import Event

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub

__all__ = [
    "BucketSize",
    "RollupDelta",
    "RollupKey",
    "RollupPoint",
    "RollupRow",
    "StatsRollups",
    "bucket_start",
    "event_contributions",
    "next_bucket",
]


class BucketSize(StrEnum):
    MINUTE = "1m"
    HOUR = "1h"
    DAY = "1d"


@dataclass(frozen=True, slots=True)
class RollupKey:
    space_id: UUID
    service: str
    metric: str
    dims: tuple[tuple[str, str], ...]
    bucket_start: datetime
    bucket_size: BucketSize

    @property
    def dimensions(self) -> dict[str, str]:
        return dict(self.dims)


@dataclass(frozen=True, slots=True)
class RollupRow:
    key: RollupKey
    value: float


@dataclass(frozen=True, slots=True)
class RollupDelta:
    row: RollupRow
    additive: bool


@dataclass(frozen=True, slots=True)
class RollupPoint:
    bucket_start: datetime
    value: float


def bucket_start(value: datetime, size: BucketSize, timezone: str) -> datetime:
    value = value.astimezone(UTC)
    if size is BucketSize.MINUTE:
        return value.replace(second=0, microsecond=0)
    if size is BucketSize.HOUR:
        return value.replace(minute=0, second=0, microsecond=0)
    local = value.astimezone(ZoneInfo(timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _dim(**values: Any) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in values.items() if value is not None))


def event_contributions(event: Event) -> tuple[tuple[str, tuple[tuple[str, str], ...], float], ...]:
    """Map one immutable event to bounded analytics metrics."""
    data = event.data
    items: list[tuple[str, tuple[tuple[str, str], ...], float]] = [
        ("events.total", _dim(type=event.type), 1.0)
    ]
    if event.type == "identity.registered":
        items.append(("identities.added", _dim(method="register"), 1.0))
    elif event.type == "identity.imported":
        items.append(("identities.added", _dim(method="import"), float(data.get("count", 0))))
    elif event.type == "identity.auth.changed":
        if data.get("to") == "authenticated":
            items.append(("auth.authenticated", (), 1.0))
        if data.get("to") == "session_invalid":
            items.append(("auth.session_lost", (), 1.0))
    elif event.type == "identity.login.succeeded":
        items.append(("logins.succeeded", (), 1.0))
    elif event.type == "identity.login.failed":
        items.append(("logins.failed", _dim(code=data.get("code") or data.get("error")), 1.0))
    elif event.type.startswith("identity.challenge."):
        outcome = event.type.removeprefix("identity.challenge.")
        if outcome in {"opened", "answered", "expired"}:
            items.append((f"challenges.{outcome}", _dim(kind=data.get("kind")), 1.0))
    elif event.type == "identity.verification.changed":
        if data.get("to") == "verified":
            items.append(("verifications.verified", _dim(factor=data.get("key")), 1.0))
        elif data.get("to") == "rejected":
            items.append(
                (
                    "verifications.rejected",
                    _dim(factor=data.get("key"), code=data.get("code")),
                    1.0,
                )
            )
    elif event.type == "identity.tier.reached":
        items.append(("tiers.reached", _dim(tier=data.get("tier")), 1.0))
    elif event.type == "identity.tier.lost":
        items.append(("tiers.lost", _dim(tier=data.get("tier")), 1.0))
    elif event.type == "identity.standing.changed":
        target = data.get("to")
        source = data.get("from")
        if target in {"banned", "frozen", "limited"}:
            items.append(("standings.entered", _dim(state=target, code=data.get("code")), 1.0))
        if source in {"banned", "frozen"} and target not in {"banned", "frozen"}:
            items.append(("standings.recovered", _dim(state=source), 1.0))
    elif event.type == "identity.state.overridden":
        items.append(("state.overrides", _dim(region=data.get("region")), 1.0))
    elif event.type == "identity.state.drift":
        items.append(("state.drift", _dim(region=data.get("region")), 1.0))
    elif event.type == "identity.availability.changed" and data.get("to") == "cooling":
        items.append(("availability.cooling", (), 1.0))
    elif event.type == "identity.quota.exhausted":
        items.append(("quotas.exhausted", _dim(kind=data.get("kind")), 1.0))
    elif event.type == "identity.lifecycle.changed" and data.get("to") == "retired":
        items.append(("lifecycle.retired", (), 1.0))
    elif event.type == "lease.acquired":
        items.append(("leases.acquired", _dim(pool=data.get("pool")), 1.0))
    elif event.type in {
        "lease.released",
        "lease.failed",
        "lease.lost",
        "lease.expired",
        "lease.revoked",
    }:
        items.append(
            (
                "leases.ended",
                _dim(pool=data.get("pool"), reason=data.get("reason", event.type.removeprefix("lease."))),
                1.0,
            )
        )
    elif event.type == "operation.succeeded":
        items.append(("operations.succeeded", _dim(kind=data.get("kind")), 1.0))
    elif event.type == "operation.failed":
        items.append(
            (
                "operations.failed",
                _dim(kind=data.get("kind"), blame=data.get("blame"), code=data.get("code")),
                1.0,
            )
        )
    elif event.type == "proxy.status.changed":
        items.append(("proxies.status", _dim(pool=data.get("pool"), state=data.get("to")), 1.0))
    return tuple(items)


def _keys(
    event: Event, space_id: Any, timezone: str, sizes: tuple[BucketSize, ...] = tuple(BucketSize)
) -> tuple[tuple[RollupKey, float], ...]:
    result: list[tuple[RollupKey, float]] = []
    service = event.service or ""
    for metric, dims, value in event_contributions(event):
        for size in sizes:
            result.append(
                (
                    RollupKey(
                        space_id,
                        service,
                        metric,
                        dims,
                        bucket_start(event.occurred_at, size, timezone),
                        size,
                    ),
                    value,
                )
            )
    return tuple(result)


def _minute_ranges(minutes: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Соседние минуты — одним диапазоном `[start, end)`: меньше запросов к журналу."""
    ranges: list[tuple[datetime, datetime]] = []
    for minute in minutes:
        if ranges and ranges[-1][1] == minute:
            ranges[-1] = (ranges[-1][0], minute + timedelta(minutes=1))
        else:
            ranges.append((minute, minute + timedelta(minutes=1)))
    return ranges


def _summed(rows: list[RollupRow], start: datetime, size: BucketSize) -> list[RollupRow]:
    """Сложить мелкие корзины в одну крупную с началом `start`."""
    totals: defaultdict[RollupKey, float] = defaultdict(float)
    for row in rows:
        key = row.key
        totals[RollupKey(key.space_id, key.service, key.metric, key.dims, start, size)] += row.value
    return [RollupRow(key, value) for key, value in totals.items()]


class StatsRollups:
    """Dispatcher consumer and rebuild facade; reads never scan the event journal."""

    def __init__(self, hub: IdentityHub) -> None:
        self.hub = hub
        self._last_purge: float | None = None

    async def handle_events(self, events: list[Event]) -> None:
        """Пересчитать корзины, затронутые пачкой событий. Идемпотентно: значения заменяются.

        Из журнала перечитываются только затронутые минуты, а не сутки: часовые корзины
        собираются из минутных, суточные — из часовых (R2-F11). Так стоимость пачки не растёт
        с числом событий за день. Для событий старше хранимых минутных корзин час
        пересчитывается по журналу; сутки старше хранимых часовых корзин — тоже по журналу.
        """
        if not events:
            return
        spaces = {space.slug: space for space in await self.hub._store.list_spaces()}
        by_space: defaultdict[str, list[Event]] = defaultdict(list)
        for event in events:
            if event.space in spaces:
                by_space[event.space].append(event)
        now = await self.hub._store.current_time()  # как и чистка корзин — по времени хранилища
        fresh_after = now - self.hub.retention.rollup_minutes + timedelta(hours=1)
        # сутки с запасом: часовые корзины суток либо все на месте, либо сутки считаются по журналу
        hours_after = now - self.hub.retention.rollup_hours + timedelta(days=2)
        for slug, items in by_space.items():
            await self._recompute(spaces[slug], items, fresh_after, hours_after)

    async def _recompute(
        self, space: Any, events: list[Event], fresh_after: datetime, hours_after: datetime
    ) -> None:
        store = self.hub._store
        minutes = sorted(
            {bucket_start(event.occurred_at, BucketSize.MINUTE, space.timezone) for event in events}
        )
        totals: defaultdict[RollupKey, float] = defaultdict(float)
        for start, end in _minute_ranges(minutes):
            for event in await store.rollup_source_events(space.slug, start, end):
                for key, value in _keys(event, space.id, space.timezone, (BucketSize.MINUTE,)):
                    totals[key] += value
        await store.replace_rollups([RollupRow(key, value) for key, value in totals.items()])

        hours = sorted({bucket_start(minute, BucketSize.HOUR, space.timezone) for minute in minutes})
        rows: list[RollupRow] = []
        for hour in hours:
            end = hour + timedelta(hours=1)
            if hour >= fresh_after:
                rows += _summed(
                    await store.event_rollup_rows(space.id, BucketSize.MINUTE, hour, end),
                    hour,
                    BucketSize.HOUR,
                )
            else:  # минутные корзины этого часа уже очищены
                rows += await self._from_journal(space, hour, end, BucketSize.HOUR)
        await store.replace_rollups(rows)

        days = sorted({bucket_start(minute, BucketSize.DAY, space.timezone) for minute in minutes})
        rows = []
        for day in days:
            end = next_bucket(day, BucketSize.DAY, space.timezone)
            if day.minute == 0 and end.minute == 0 and day >= hours_after:
                # сутки пространства состоят из целых часов UTC, и их часовые корзины ещё хранятся
                rows += _summed(
                    await store.event_rollup_rows(space.id, BucketSize.HOUR, day, end), day, BucketSize.DAY
                )
            elif day >= fresh_after:  # смещение пояса на полчаса: собираем из минут
                rows += _summed(
                    await store.event_rollup_rows(space.id, BucketSize.MINUTE, day, end), day, BucketSize.DAY
                )
            else:
                rows += await self._from_journal(space, day, end, BucketSize.DAY)
        await store.replace_rollups(rows)

    async def _from_journal(
        self, space: Any, start: datetime, end: datetime, size: BucketSize
    ) -> list[RollupRow]:
        totals: defaultdict[RollupKey, float] = defaultdict(float)
        for event in await self.hub._store.rollup_source_events(space.slug, start, end):
            for key, value in _keys(event, space.id, space.timezone, (size,)):
                totals[key] += value
        return [RollupRow(key, value) for key, value in totals.items()]

    async def rebuild(self, space_slug: str, start: datetime, end: datetime, *, force: bool = False) -> int:
        """Перестроить корзины событий диапазона по журналу; телеметрия не затрагивается.

        Диапазон, уходящий за срок хранения журнала (`Retention.events`), по журналу уже не
        восстановить: перестройка стёрла бы историю. Такой вызов требует `force=True`.
        """
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValidationFailed("rollup rebuild requires an aware, non-empty time range")
        if not force and start < await self.hub._store.current_time() - self.hub.retention.events:
            raise ValidationFailed(
                "rollup rebuild: диапазон старше срока хранения событий — перестройка сотрёт историю; "
                "передайте force=True, если это и нужно"
            )
        spaces = {space.slug: space for space in await self.hub._store.list_spaces()}
        space = spaces.get(space_slug)
        if space is None:
            raise ValidationFailed(f"unknown space: {space_slug}")
        starts = [bucket_start(start, size, space.timezone) for size in BucketSize]
        last = end - timedelta(microseconds=1)
        ends = [
            next_bucket(bucket_start(last, size, space.timezone), size, space.timezone) for size in BucketSize
        ]
        expanded_start, expanded_end = min(starts), max(ends)
        events = await self.hub._store.rollup_source_events(space_slug, expanded_start, expanded_end)
        totals: defaultdict[RollupKey, float] = defaultdict(float)
        for event in events:
            for key, value in _keys(event, space.id, space.timezone):
                totals[key] += value
        await self.hub._store.clear_rollups(space.id, expanded_start, expanded_end)
        rows = [RollupRow(key, value) for key, value in totals.items()]
        await self.hub._store.replace_rollups(rows)
        return len(rows)

    async def purge_minutes(self, *, before: datetime | None = None) -> int:
        cutoff = before or (await self.hub._store.current_time() - self.hub.retention.rollup_minutes)
        return await self.hub._store.purge_minute_rollups(cutoff)

    async def run_due(self) -> int:
        now = self.hub.clock.monotonic()
        if self._last_purge is not None and now - self._last_purge < 3600:
            return 0
        purged = await self.purge_minutes()
        self._last_purge = now
        return purged

    async def rows(
        self,
        space_slug: str,
        *,
        metric: str,
        bucket_size: BucketSize,
        start: datetime,
        end: datetime,
        service: str | None = None,
        dims: Mapping[str, str] | None = None,
    ) -> list[RollupRow]:
        return await self.hub._store.read_rollups(
            space_slug,
            metric=metric,
            bucket_size=bucket_size,
            start=start,
            end=end,
            service=service,
            dims=tuple(sorted((dims or {}).items())) if dims is not None else None,
        )

    async def series(
        self,
        space_slug: str,
        *,
        metric: str,
        bucket_size: BucketSize,
        start: datetime,
        end: datetime,
        service: str | None = None,
        dims: Mapping[str, str] | None = None,
    ) -> list[RollupPoint]:
        """Return a dense series; missing persisted buckets are explicit zeroes."""
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValidationFailed("rollup series requires an aware, non-empty time range")
        spaces = {space.slug: space for space in await self.hub._store.list_spaces()}
        space = spaces.get(space_slug)
        if space is None:
            raise ValidationFailed(f"unknown space: {space_slug}")
        first_bucket = bucket_start(start, bucket_size, space.timezone)
        rows = await self.rows(
            space_slug,
            metric=metric,
            bucket_size=bucket_size,
            start=first_bucket,
            end=end,
            service=service,
            dims=dims,
        )
        totals: defaultdict[datetime, float] = defaultdict(float)
        for row in rows:
            totals[row.key.bucket_start] += row.value
        cursor = first_bucket
        result: list[RollupPoint] = []
        while cursor < end:
            result.append(RollupPoint(cursor, totals[cursor]))
            cursor = next_bucket(cursor, bucket_size, space.timezone)
        return result


def next_bucket(start: datetime, size: BucketSize, timezone: str) -> datetime:
    if size is BucketSize.MINUTE:
        return start + timedelta(minutes=1)
    if size is BucketSize.HOUR:
        return start + timedelta(hours=1)
    local = start.astimezone(ZoneInfo(timezone))
    return (local + timedelta(days=1)).astimezone(UTC)
