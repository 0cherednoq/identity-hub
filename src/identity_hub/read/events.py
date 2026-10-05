"""Scoped, secret-free event journal read models for administration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import CATALOG, scrub
from identity_hub.store.base import EventReadFilter, EventReadRow

_SEVERITIES = frozenset({"info", "warning", "critical"})
_SUMMARY_KEYS = ("status", "state", "code", "kind", "pool", "factor", "outcome", "operation")


@dataclass(frozen=True, slots=True)
class EventFilters:
    type: str | None = None
    group: str | None = None
    severity: str | None = None
    service: str | None = None
    identity_id: UUID | None = None
    proxy_id: UUID | None = None
    lease_id: UUID | None = None
    actor: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    include_journal: bool = False


@dataclass(frozen=True, slots=True)
class EventItem:
    event_id: UUID
    type: str
    group: str
    severity: str
    service: str | None
    identity_id: UUID | None
    proxy_id: UUID | None
    lease_id: UUID | None
    sequence: int | None
    actor: str
    summary: str
    data: Mapping[str, Any]
    external: bool
    occurred_at: datetime
    revision: int | None = None  # ревизия identity после команды (IMP-07)


@dataclass(frozen=True, slots=True)
class EventPage:
    items: tuple[EventItem, ...]
    next_before: tuple[datetime, UUID] | None


def event_groups() -> tuple[str, ...]:
    return tuple(sorted({spec.group for spec in CATALOG.values()}))


def _allowed_types(filters: EventFilters) -> frozenset[str]:
    if filters.type is not None and filters.type not in CATALOG:
        raise ValidationFailed("неизвестный тип события")
    groups = set(event_groups())
    if filters.group is not None and filters.group not in groups:
        raise ValidationFailed("неизвестная группа событий")
    allowed = {event_type for event_type, spec in CATALOG.items() if filters.include_journal or spec.external}
    if filters.type is not None:
        allowed.intersection_update({filters.type})
    if filters.group is not None:
        allowed.intersection_update(
            event_type for event_type, spec in CATALOG.items() if spec.group == filters.group
        )
    return frozenset(allowed)


def _validate(filters: EventFilters, limit: int) -> None:
    if not 1 <= limit <= 200:
        raise ValidationFailed("limit: от 1 до 200")
    if filters.severity is not None and filters.severity not in _SEVERITIES:
        raise ValidationFailed("неизвестная важность события")
    if filters.occurred_from is not None and filters.occurred_from.tzinfo is None:
        raise ValidationFailed("occurred_from должен содержать часовой пояс")
    if filters.occurred_to is not None and filters.occurred_to.tzinfo is None:
        raise ValidationFailed("occurred_to должен содержать часовой пояс")
    if (
        filters.occurred_from is not None
        and filters.occurred_to is not None
        and filters.occurred_from > filters.occurred_to
    ):
        raise ValidationFailed("начало диапазона позже конца")


def _summary(event_type: str, data: Mapping[str, Any]) -> str:
    parts = []
    for key in _SUMMARY_KEYS:
        value = data.get(key)
        if value is None or isinstance(value, Mapping | list | tuple):
            continue
        rendered = str(value)
        if len(rendered) <= 80:
            parts.append(f"{key}={rendered}")
        if len(parts) == 3:
            break
    return " · ".join(parts) if parts else event_type


def _item(row: EventReadRow) -> EventItem:
    data = scrub(row.data)
    return EventItem(
        event_id=row.event_id,
        type=row.type,
        group=CATALOG[row.type].group,
        severity=row.severity,
        service=row.service,
        identity_id=row.identity_id,
        proxy_id=row.proxy_id,
        lease_id=row.lease_id,
        sequence=row.sequence,
        actor=row.actor,
        summary=_summary(row.type, data),
        data=data,
        external=row.external,
        occurred_at=row.occurred_at,
        revision=row.revision,
    )


class EventsReadMixin(SpaceBase):
    async def _event_page(
        self,
        *,
        filters: EventFilters | None = None,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> EventPage:
        filters = filters or EventFilters()
        _validate(filters, limit)
        async with self._tx(own=True) as tx:
            rows = await tx.event_read_page(
                EventReadFilter(
                    allowed_types=_allowed_types(filters),
                    severities=frozenset() if filters.severity is None else frozenset({filters.severity}),
                    service=filters.service,
                    identity_id=filters.identity_id,
                    proxy_id=filters.proxy_id,
                    lease_id=filters.lease_id,
                    actor=filters.actor,
                    occurred_from=filters.occurred_from,
                    occurred_to=filters.occurred_to,
                ),
                limit=limit + 1,
                before=before,
            )
        visible = rows[:limit]
        return EventPage(
            items=tuple(_item(row) for row in visible),
            next_before=(visible[-1].occurred_at, visible[-1].event_id)
            if len(rows) > limit and visible
            else None,
        )

    async def _event_detail(self, event_id: UUID) -> EventItem:
        async with self._tx(own=True) as tx:
            row = await tx.event_read_one(event_id)
        if row is None:
            raise NotFound("event", event_id)
        return _item(row)
