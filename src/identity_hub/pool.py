"""Пул: выборка аккаунтов сервиса в пространстве + политики аренды (ТЗ §2, FR-6.1, G-1.2)."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from identity_hub.domain.admissibility import compile_rule
from identity_hub.domain.data import validate_labels
from identity_hub.enums import Standing
from identity_hub.errors import ValidationFailed
from identity_hub.service import LeaseMode, Pacing, ProxyPolicy, Service
from identity_hub.store.base import CandidateQuery, IdentityRow

if TYPE_CHECKING:
    from identity_hub._space import SpaceBase

__all__ = ["DEFAULT_STANDING", "LeaseTiming", "Pool", "PoolsMixin", "default_holder"]

DEFAULT_STANDING = frozenset({Standing.GOOD, Standing.LIMITED})


def default_holder() -> str:
    """hostname:pid — кто держит аренду (для админки)."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class LeaseTiming:
    """TTL, heartbeat и запас самоограждения (ТЗ FR-6.2, 6.3, G-1.2, G-1.3)."""

    ttl: timedelta = timedelta(seconds=60)
    heartbeat: timedelta = timedelta(seconds=20)
    safety_margin: timedelta = timedelta(seconds=20)

    def __post_init__(self) -> None:
        if self.heartbeat * 3 > self.ttl:
            raise ValidationFailed("heartbeat должен быть ≤ TTL/3 (G-1.2)")
        if self.safety_margin < self.heartbeat:
            raise ValidationFailed("safety_margin должен быть ≥ heartbeat (G-1.2)")
        if self.safety_margin >= self.ttl:
            raise ValidationFailed("safety_margin должен быть меньше TTL")


@dataclass
class Pool:
    """Выборка аккаунтов сервиса по тегам и политикам для конкретного применения."""

    space: SpaceBase
    name: str
    service: Service
    tags: frozenset[str] = frozenset()
    labels: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    pacing: Pacing | None = None
    proxy_policy: ProxyPolicy | None = None
    timing: LeaseTiming = field(default_factory=LeaseTiming)
    holder: str = field(default_factory=default_holder)
    capacity_low: int | None = None  # порог события pool.capacity.low (P8.2)

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValidationFailed("имя пула: от 1 до 128 символов")
        self.labels = dict(validate_labels(self.labels))
        _check_profile_filter(self.service, self.profile)
        if self.proxy_policy is None:
            self.proxy_policy = self.service.proxy_policy

    @property
    def shared(self) -> bool:
        return self.service.lease_mode is LeaseMode.SHARED

    def candidate_query(
        self,
        *,
        for_operation: str | None = None,
        tags: Iterable[str] = (),
        labels: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        among: Any = None,
        exclude: Iterable[UUID] = (),
    ) -> CandidateQuery:
        """Фильтры пула + фильтры вызова; `for_operation` компилирует правило операции в выборку."""
        if for_operation is not None:
            rule = compile_rule(self.service, for_operation)
            standing, needs_auth, factors = rule.standing, rule.needs_auth, rule.required_factors
        else:
            standing, needs_auth, factors = DEFAULT_STANDING, True, frozenset[str]()
        merged_labels = {**self.labels, **validate_labels(labels or {})}
        merged_profile = {**self.profile, **(profile or {})}
        _check_profile_filter(self.service, merged_profile)
        return CandidateQuery(
            service=self.service.name,
            standing=frozenset(standing),
            needs_auth=needs_auth,
            required_factors=frozenset(factors),
            tags=self.tags | frozenset(tags),
            labels=merged_labels or None,
            profile=merged_profile or None,
            among=among,
            exclude=frozenset(exclude),
            shared=self.shared,
        )

    async def _pick(self, **filters: Any) -> IdentityRow | None:
        """Выбрать кандидата в короткой транзакции (без захвата аренды) — для проверки фильтров."""
        query = self.candidate_query(**filters)
        async with self.space._tx(own=True) as tx:
            return await tx.pick_candidate(query)


def _check_profile_filter(service: Service, profile: Mapping[str, Any]) -> None:
    unknown = set(profile) - service.filterable_profile
    if unknown:
        raise ValidationFailed(f"фильтр профиля: поля не объявлены filterable: {sorted(unknown)}")


class PoolsMixin:
    def pool(
        self: Any,
        name: str,
        *,
        service: Service | str,
        tags: Iterable[str] = (),
        labels: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        pacing: Pacing | None = None,
        proxy_policy: ProxyPolicy | None = None,
        ttl: timedelta | None = None,
        heartbeat: timedelta | None = None,
        safety_margin: timedelta | None = None,
        holder: str | None = None,
        capacity_low: int | None = None,
    ) -> Pool:
        defaults = LeaseTiming()
        timing = LeaseTiming(
            ttl=ttl or defaults.ttl,
            heartbeat=heartbeat or defaults.heartbeat,
            safety_margin=safety_margin or defaults.safety_margin,
        )
        return Pool(
            space=self,
            name=name,
            service=self.hub.service(service),
            tags=frozenset(tags),
            labels=dict(labels or {}),
            profile=dict(profile or {}),
            pacing=pacing,
            proxy_policy=proxy_policy,
            timing=timing,
            holder=holder or default_holder(),
            capacity_low=capacity_low,
        )
