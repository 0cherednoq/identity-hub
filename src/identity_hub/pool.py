"""Пул: выборка аккаунтов сервиса в пространстве + политики аренды (ТЗ §2, FR-6.1, G-1.2)."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from identity_hub.domain.admissibility import compile_rule
from identity_hub.domain.data import validate_labels
from identity_hub.enums import Standing
from identity_hub.errors import ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.service import LeaseMode, Pacing, ProxyDownPolicy, ProxyPolicy, Service
from identity_hub.store.base import CandidateQuery, IdentityRow

if TYPE_CHECKING:
    from identity_hub._space import SpaceBase
    from identity_hub.lease import Lease, OnLost

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
    _capacity_state: str = field(default="ok", init=False, repr=False)

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
            proxy_wait=bool(
                self.proxy_policy
                and self.proxy_policy.sticky
                and self.proxy_policy.on_proxy_down is ProxyDownPolicy.WAIT
            ),
        )

    def lease(self, *, on_lost: OnLost | None = None, **kwargs: Any) -> AbstractAsyncContextManager[Lease]:
        """`async with pool.lease(for_operation=..., wait=..., on_lost=...) as lease:` (ТЗ §11)."""
        from identity_hub.lease import lease_scope

        return lease_scope(self, on_lost, **kwargs)

    async def acquire(self, **kwargs: Any) -> Lease:
        """Низкоуровневый захват (без heartbeat); обычный путь — `async with pool.lease(...)`."""
        from identity_hub.lease import acquire

        return await acquire(self, **kwargs)

    async def check_capacity(self) -> int:
        """Свободная ёмкость пула; `pool.capacity.low/empty` — при смене состояния (FR-13.3, D-119)."""
        threshold = self.capacity_low or 0
        async with self.space._tx(own=True) as tx:
            available = await tx.count_candidates(self.candidate_query(), threshold + 1)
            state = "empty" if available == 0 else ("low" if available <= threshold else "ok")
            if state != self._capacity_state:
                if state != "ok":
                    await self.space._emit(
                        tx,
                        EventDraft(
                            f"pool.capacity.{state}",
                            actor=self.holder,
                            service=self.service.name,
                            data={"pool": self.name, "available": available, "threshold": self.capacity_low},
                        ),
                    )
                self._capacity_state = state
        return available

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
    async def revoke_lease(self: Any, identity_id: UUID, *, reason: str, actor: str) -> bool:
        """Отозвать аренду из админки (FR-6.9): воркер теряет её на ближайшем heartbeat,
        а его записи отклоняются по fencing. False — живой аренды не было."""
        if not reason:
            raise ValidationFailed("отзыв аренды требует причину")
        async with self._tx(own=True) as tx:
            row = await self._identity(tx, identity_id)
            revoked = await tx.revoke_lease(identity_id)
            if revoked is None:
                return False
            await self._emit(
                tx,
                EventDraft(
                    "lease.revoked",
                    actor=actor,
                    service=row.service,
                    identity_id=identity_id,
                    lease_id=revoked.lease_id,
                    data={"pool": revoked.pool, "holder": revoked.holder, "reason": reason},
                ),
            )
            return True

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
