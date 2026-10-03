"""Secret-free proxy overview, cards, bindings and binding history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.store.base import (
    ProxyBindingHistoryRow,
    ProxyBoundIdentityRow,
    ProxyPoolCapacityRow,
    ProxyReadRow,
)

ProxyCapacity = Literal["available", "full", "unbound"]


@dataclass(frozen=True, slots=True)
class ProxyFilters:
    pool: str | None = None
    status: str | None = None
    capacity: ProxyCapacity | None = None


@dataclass(frozen=True, slots=True)
class ProxyCard:
    id: UUID
    pool: str
    scheme: str
    host: str
    port: int
    masked_url: str
    status: str
    error_score: float
    fail_streak: int
    next_check_at: datetime | None
    status_changed_at: datetime
    status_source: str
    checked_at: datetime | None
    pinned_until: datetime | None
    pending_observation: str | None
    max_identities: int
    bound_count: int
    available_capacity: int
    capacity_utilization: float
    in_lease_count: int
    created_at: datetime
    actions_available: bool
    actions_unavailable_reason: str


@dataclass(frozen=True, slots=True)
class ProxyPoolCapacity:
    pool: str
    proxies: int
    alive: int
    quarantined: int
    dead: int
    other: int
    max_identities: int
    bound_count: int
    available_capacity: int
    in_lease_count: int
    utilization: float


@dataclass(frozen=True, slots=True)
class ProxyPage:
    items: tuple[ProxyCard, ...]
    pools: tuple[ProxyPoolCapacity, ...]
    next_before: tuple[datetime, UUID] | None


@dataclass(frozen=True, slots=True)
class ProxyBindingHistoryPage:
    items: tuple[ProxyBindingHistoryRow, ...]
    next_before: tuple[datetime, UUID] | None


@dataclass(frozen=True, slots=True)
class ProxyDetail:
    proxy: ProxyCard
    bound_identities: tuple[ProxyBoundIdentityRow, ...]
    history: ProxyBindingHistoryPage


def _card(row: ProxyReadRow) -> ProxyCard:
    available = max(0, row.max_identities - row.bound_count)
    return ProxyCard(
        id=row.id,
        pool=row.pool,
        scheme=row.scheme,
        host=row.host,
        port=row.port,
        masked_url=f"{row.scheme}://{row.host}:{row.port}",
        status=row.status,
        error_score=row.error_score,
        fail_streak=row.fail_streak,
        next_check_at=row.next_check_at,
        status_changed_at=row.status_changed_at,
        status_source=row.status_source,
        checked_at=row.checked_at,
        pinned_until=row.pinned_until,
        pending_observation=row.pending_observation,
        max_identities=row.max_identities,
        bound_count=row.bound_count,
        available_capacity=available,
        capacity_utilization=row.bound_count / row.max_identities,
        in_lease_count=row.in_lease_count,
        created_at=row.created_at,
        actions_available=False,
        actions_unavailable_reason="Действия станут доступны после A3.4.",
    )


def _pool(row: ProxyPoolCapacityRow) -> ProxyPoolCapacity:
    available = max(0, row.max_identities - row.bound_count)
    return ProxyPoolCapacity(
        pool=row.pool,
        proxies=row.proxies,
        alive=row.alive,
        quarantined=row.quarantined,
        dead=row.dead,
        other=row.other,
        max_identities=row.max_identities,
        bound_count=row.bound_count,
        available_capacity=available,
        in_lease_count=row.in_lease_count,
        utilization=row.bound_count / row.max_identities if row.max_identities else 0.0,
    )


def _validate(limit: int, filters: ProxyFilters) -> None:
    if not 1 <= limit <= 200:
        raise ValidationFailed("limit: от 1 до 200")
    if filters.capacity not in (None, "available", "full", "unbound"):
        raise ValidationFailed("неизвестный фильтр capacity")
    if filters.status not in (None, "alive", "quarantined", "dead", "unknown"):
        raise ValidationFailed("неизвестный статус прокси")


class ProxyReadMixin(SpaceBase):
    async def _proxy_overview(
        self,
        *,
        filters: ProxyFilters | None = None,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> ProxyPage:
        filters = filters or ProxyFilters()
        _validate(limit, filters)
        async with self._tx(own=True) as tx:
            rows = await tx.proxy_read_page(
                limit=limit + 1,
                before=before,
                pool=filters.pool,
                status=filters.status,
                capacity=filters.capacity,
            )
            pools = await tx.proxy_pool_capacities()
        visible = rows[:limit]
        return ProxyPage(
            items=tuple(_card(row) for row in visible),
            pools=tuple(_pool(row) for row in pools),
            next_before=(visible[-1].created_at, visible[-1].id) if len(rows) > limit and visible else None,
        )

    async def _proxy_detail(
        self,
        proxy_id: UUID,
        *,
        history_limit: int = 50,
        history_before: tuple[datetime, UUID] | None = None,
    ) -> ProxyDetail:
        if not 1 <= history_limit <= 200:
            raise ValidationFailed("history_limit: от 1 до 200")
        async with self._tx(own=True) as tx:
            row = await tx.proxy_read_one(proxy_id)
            if row is None:
                raise NotFound("proxy", proxy_id)
            bound = await tx.proxy_bound_identities(proxy_id)
            history = await tx.proxy_binding_history(
                proxy_id,
                limit=history_limit + 1,
                before=history_before,
            )
        visible = history[:history_limit]
        return ProxyDetail(
            proxy=_card(row),
            bound_identities=tuple(bound),
            history=ProxyBindingHistoryPage(
                items=tuple(visible),
                next_before=(visible[-1].occurred_at, visible[-1].event_id)
                if len(history) > history_limit and visible
                else None,
            ),
        )
