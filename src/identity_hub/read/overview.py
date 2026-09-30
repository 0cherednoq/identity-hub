"""Safe operational overview read models for spaces and pools."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.api._base import legacy
from identity_hub.errors import SpaceMismatch

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub
    from identity_hub.pool import Pool
    from identity_hub.store.base import OverviewServiceRow, OverviewSnapshot, SpaceRef

__all__ = [
    "FactorCount",
    "OverviewMixin",
    "PoolCapacity",
    "RequirementCount",
    "ServiceOverview",
    "SpaceInfo",
    "SpaceOverview",
    "SpacesMatrix",
    "TierCount",
    "list_spaces",
    "spaces_matrix",
]


@dataclass(frozen=True, slots=True)
class SpaceInfo:
    id: UUID
    slug: str
    title: str
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class FactorCount:
    factor: str
    states: dict[str, int]


@dataclass(frozen=True, slots=True)
class TierCount:
    tier: str
    count: int


@dataclass(frozen=True, slots=True)
class RequirementCount:
    code: str
    count: int
    next_deadline: datetime | None


@dataclass(frozen=True, slots=True)
class ServiceOverview:
    service: str
    title: str
    total: int
    eligible: int
    leased: int
    free_eligible: int
    heavy: int
    open_challenges: int
    action_required: int
    added_24h: int
    added_7d: int
    regions: dict[str, dict[str, int]]
    factors: tuple[FactorCount, ...]
    tiers: tuple[TierCount, ...]
    requirements: tuple[RequirementCount, ...]


@dataclass(frozen=True, slots=True)
class SpaceOverview:
    space: SpaceInfo
    total: int
    eligible: int
    leased: int
    free_eligible: int
    heavy: int
    open_challenges: int
    action_required: int
    added_24h: int
    added_7d: int
    services: tuple[ServiceOverview, ...]


@dataclass(frozen=True, slots=True)
class SpacesMatrix:
    spaces: tuple[SpaceOverview, ...]
    service: str | None = None


@dataclass(frozen=True, slots=True)
class PoolCapacity:
    pool: str
    service: str
    eligible: int
    leased: int
    free: int
    low_threshold: int | None
    state: str


def _space_info(ref: SpaceRef) -> SpaceInfo:
    return SpaceInfo(ref.id, ref.slug, ref.title, ref.timezone)


async def list_spaces(
    hub: IdentityHub, allowed_spaces: Collection[str] | None = None
) -> tuple[SpaceInfo, ...]:
    """List spaces after applying the caller's authorization scope."""
    allowed = None if allowed_spaces is None or "*" in allowed_spaces else frozenset(allowed_spaces)
    refs = await hub.store.list_spaces(allowed)
    return tuple(_space_info(ref) for ref in refs)


async def spaces_matrix(
    hub: IdentityHub,
    allowed_spaces: Collection[str] | None = None,
    *,
    service: str | None = None,
) -> SpacesMatrix:
    """Build current overviews only for already-authorized spaces."""
    if service is not None:
        hub.service(service)
    spaces = await list_spaces(hub, allowed_spaces)
    overviews: list[SpaceOverview] = []
    for space in spaces:
        handle = hub.space(space.slug, space.title, space_id=space.id)
        overview = await handle._space_overview(service=service)
        overviews.append(overview)
    return SpacesMatrix(tuple(overviews), service)


def _regions(rows: list[tuple[str, str, str, int]], service: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = defaultdict(dict)
    for row_service, region, state, count in rows:
        if row_service == service:
            result["auth" if region == "auth_state" else region][state] = count
    return dict(result)


def _factor_counts(
    hub: IdentityHub,
    snapshot: OverviewSnapshot,
    aggregate: OverviewServiceRow,
) -> tuple[FactorCount, ...]:
    service = hub.service(aggregate.service)
    states: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row_service, factor, state, count in snapshot.verification_states:
        if row_service == service.name:
            states[factor][state] += count
    result: list[FactorCount] = []
    for factor in sorted(service.factor_names):
        values = dict(states.get(factor, {}))
        accounted = sum(values.values())
        values["missing"] = values.get("missing", 0) + max(aggregate.total - accounted, 0)
        result.append(FactorCount(factor, values))
    return tuple(result)


def _tier_counts(
    hub: IdentityHub,
    snapshot: OverviewSnapshot,
    aggregate: OverviewServiceRow,
) -> tuple[TierCount, ...]:
    service = hub.service(aggregate.service)
    groups = [
        (frozenset(factors), count)
        for row_service, factors, count in snapshot.factor_sets
        if row_service == service.name
    ]
    return tuple(
        TierCount(tier.name, sum(count for factors, count in groups if tier.factors <= factors))
        for tier in service.tiers
    )


def _service_overview(
    hub: IdentityHub,
    snapshot: OverviewSnapshot,
    region_rows: list[tuple[str, str, str, int]],
    aggregate: OverviewServiceRow,
) -> ServiceOverview:
    service = hub.service(aggregate.service)
    requirements = tuple(
        RequirementCount(row.code, row.count, row.next_deadline)
        for row in snapshot.requirements
        if row.service == service.name
    )
    return ServiceOverview(
        service=service.name,
        title=service.title,
        total=aggregate.total,
        eligible=aggregate.eligible,
        leased=aggregate.leased,
        free_eligible=max(aggregate.eligible - aggregate.leased, 0),
        heavy=aggregate.heavy,
        open_challenges=aggregate.open_challenges,
        action_required=aggregate.action_required,
        added_24h=aggregate.added_24h,
        added_7d=aggregate.added_7d,
        regions=_regions(region_rows, service.name),
        factors=_factor_counts(hub, snapshot, aggregate),
        tiers=_tier_counts(hub, snapshot, aggregate),
        requirements=requirements,
    )


class OverviewMixin(SpaceBase):
    async def _space_overview(self, *, service: str | None = None) -> SpaceOverview:
        """Current safe counts for one space, optionally narrowed to one service."""
        ref = await self.ref()
        if service is not None:
            self.hub.service(service)
        async with self._tx(own=True) as tx:
            snapshot = await tx.overview_snapshot()
            region_rows = await tx.region_counts(service)
        services = tuple(
            _service_overview(self.hub, snapshot, region_rows, aggregate)
            for aggregate in snapshot.services
            if service is None or aggregate.service == service
        )

        def total(field: str) -> int:
            return sum(int(getattr(item, field)) for item in services)

        return SpaceOverview(
            space=_space_info(ref),
            total=total("total"),
            eligible=total("eligible"),
            leased=total("leased"),
            free_eligible=total("free_eligible"),
            heavy=total("heavy"),
            open_challenges=total("open_challenges"),
            action_required=total("action_required"),
            added_24h=total("added_24h"),
            added_7d=total("added_7d"),
            services=services,
        )

    async def _pool_capacity(self, pool: Pool) -> PoolCapacity:
        """Read pool capacity without emitting capacity transition events."""
        if pool.space.slug != self.slug:
            raise SpaceMismatch(f"pool {pool.name!r} belongs to space {pool.space.slug!r}")
        query = pool.candidate_query()
        unlimited = 2_147_483_647
        async with self._tx(own=True) as tx:
            eligible = await tx.count_candidates(replace(query, shared=True), unlimited)
            leased = await tx.count_live_candidates(query, unlimited)
        free = eligible if pool.shared else max(eligible - leased, 0)
        threshold = pool.capacity_low
        state = "empty" if free == 0 else ("low" if threshold is not None and free <= threshold else "ok")
        return PoolCapacity(pool.name, pool.service.name, eligible, leased, free, threshold, state)

    pool_capacity = legacy(_pool_capacity, "pool_capacity")
    space_overview = legacy(_space_overview, "space_overview")
