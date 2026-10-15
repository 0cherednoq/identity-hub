"""Пул: выборка аккаунтов сервиса в пространстве + политики аренды (ТЗ §2, FR-6.1, G-1.2)."""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from identity_hub.domain.admissibility import compile_rule
from identity_hub.domain.data import normalize_tag, validate_labels
from identity_hub.domain.redaction import sanitize_text
from identity_hub.enums import Standing
from identity_hub.errors import ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.metrics import record_metric
from identity_hub.service import LeaseMode, Pacing, ProxyDownPolicy, ProxyPolicy, Service
from identity_hub.store.base import CandidateQuery, IdentityRow, LeaseOptions, QuotaCharge

if TYPE_CHECKING:
    from sqlalchemy import Select

    from identity_hub.api.identities import Identity


if TYPE_CHECKING:
    from identity_hub._space import SpaceBase
    from identity_hub.lease import Lease, OnLost

__all__ = ["DEFAULT_STANDING", "LeaseTiming", "Pool", "PoolCapacity", "PoolsMixin", "default_holder"]

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
    # как часто heartbeat пишет в историю время и счётчики операций; между — только ih_leases (D-210)
    stats_flush: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.heartbeat * 3 > self.ttl:
            raise ValidationFailed("heartbeat должен быть ≤ TTL/3 (G-1.2)")
        if self.stats_flush < self.heartbeat:
            raise ValidationFailed("stats_flush должен быть ≥ heartbeat")
        if self.safety_margin < self.heartbeat:
            raise ValidationFailed("safety_margin должен быть ≥ heartbeat (G-1.2)")
        if self.safety_margin >= self.ttl:
            raise ValidationFailed("safety_margin должен быть меньше TTL")


PURPOSE_PATTERN = re.compile(r"[a-z0-9_.:-]{1,64}")
REF_LIMIT = 256


def check_purpose(value: str) -> str:
    """Метка вида работы: по ней группируют, поэтому набор символов узкий."""
    if not isinstance(value, str) or PURPOSE_PATTERN.fullmatch(value) is None:
        raise ValidationFailed("purpose: от 1 до 64 символов из a-z, 0-9 и _ . : -")
    return value


def check_ref(value: str) -> str:
    """Ссылка на задачу: свободный текст до 256 символов, без встроенных секретов."""
    if not isinstance(value, str) or not value or len(value) > REF_LIMIT:
        raise ValidationFailed(f"ref: от 1 до {REF_LIMIT} символов")
    return sanitize_text(value, limit=REF_LIMIT) or value


def check_max_hold(value: timedelta, timing: LeaseTiming) -> timedelta:
    """Потолок удержания: держатель теряет аренду за `safety_margin` до него, поэтому он длиннее запаса."""
    if not isinstance(value, timedelta) or value <= timing.safety_margin:
        raise ValidationFailed("max_hold должен быть больше safety_margin")
    return value


def check_reserve(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        raise ValidationFailed("reserve/keep_free: целое число от 0 до 1000")
    return value


def check_priority(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**31) <= value < 2**31:
        raise ValidationFailed("priority: знаковое 32-битное целое")
    return value


@dataclass(frozen=True, slots=True)
class PoolCapacity:
    """Свободная ёмкость пула до и после применения резерва."""

    free: int
    reserved: int
    available: int


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
    purpose: str | None = None  # назначение аренд пула по умолчанию (N1.1)
    max_hold: timedelta | None = None  # потолок удержания аренд пула по умолчанию (N1.2)
    reserve: int = 0
    reserve_strict: bool = False
    priority: int = 0
    yield_grace: timedelta | None = None
    _capacity_state: str = field(default="ok", init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValidationFailed("имя пула: от 1 до 128 символов")
        self.labels = dict(validate_labels(self.labels))
        if self.purpose is not None:
            self.purpose = check_purpose(self.purpose)
        if self.max_hold is not None:
            check_max_hold(self.max_hold, self.timing)
        self.reserve = check_reserve(self.reserve)
        self.priority = check_priority(self.priority)
        if self.yield_grace is not None and (
            not isinstance(self.yield_grace, timedelta) or self.yield_grace <= timedelta(0)
        ):
            raise ValidationFailed("yield_grace должен быть положительным timedelta")
        if self.reserve and self.shared:
            raise ValidationFailed("reserve не поддерживается для SHARED-пула")
        if self.priority and self.shared:
            raise ValidationFailed("priority не поддерживается для SHARED-пула")
        if self.yield_grace is not None and self.shared:
            raise ValidationFailed("yield_grace не поддерживается для SHARED-пула")
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
        # список id, подзапрос read-схемы (только PostgreSQL) или предикат от Identity (F5.1)
        among: Select[Any] | Iterable[UUID] | Callable[[Identity], bool] | None = None,
        exclude: Iterable[UUID] = (),
        require_quota: bool = False,
    ) -> CandidateQuery:
        """Фильтры пула + фильтры вызова; `for_operation` компилирует правило операции в выборку.

        `require_quota` добавляет к выборке окна квот операции: аккаунт с исчерпанным окном не выдаётся.
        """
        quotas: tuple[QuotaCharge, ...] = ()
        if for_operation is not None:
            rule = compile_rule(self.service, for_operation)
            standing, needs_auth, factors = rule.standing, rule.needs_auth, rule.required_factors
            if require_quota:
                quotas = tuple(
                    QuotaCharge(for_operation, quota.window_seconds, quota.origin, quota.limit)
                    for quota in self.service.rule(for_operation).quotas
                )
        elif require_quota:
            raise ValidationFailed("require_quota задаётся вместе с for_operation")
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
            tags=self.tags | frozenset(normalize_tag(tag) for tag in tags),
            labels=merged_labels or None,
            profile=_expand_profile_filter(merged_profile) or None,
            among=among,
            exclude=frozenset(exclude),
            shared=self.shared,
            proxy_wait=bool(
                self.proxy_policy
                and self.proxy_policy.sticky
                and self.proxy_policy.on_proxy_down is ProxyDownPolicy.WAIT
            ),
            proxy_enabled=bool(self.proxy_policy and self.proxy_policy.sticky),
            quotas=quotas,
        )

    def lease(
        self,
        *,
        for_operation: str | None = None,
        tags: Iterable[str] = (),
        labels: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        # список id, подзапрос read-схемы (только PostgreSQL) или предикат от Identity (F5.1)
        among: Select[Any] | Iterable[UUID] | Callable[[Identity], bool] | None = None,
        exclude: Iterable[UUID] = (),
        wait: timedelta | float | None = None,
        idempotency_key: str | None = None,
        on_lost: OnLost | None = None,
        on_yield_requested: Callable[[], Any] | None = None,
        purpose: str | None = None,
        ref: str | None = None,
        max_hold: timedelta | None = None,
        require_quota: bool = False,
        keep_free: int | None = None,
        priority: int | None = None,
    ) -> AbstractAsyncContextManager[Lease]:
        """`async with pool.lease(for_operation=..., wait=...) as lease:` — захват, heartbeat, освобождение.

        `for_operation` отбирает только допустимые для операции identity; `tags`/`labels`/`profile`
        сужают фильтры пула; `exclude` — не выдавать эти id; `wait` — ждать освобождения, иначе
        `LeaseUnavailable`; `idempotency_key` — повтор вернёт ту же живую аренду; `on_lost` —
        колбэк при потере аренды; `purpose` — метка вида работы (по умолчанию из пула), `ref` —
        ссылка на задачу: оба значения видны в истории, событиях и админке; `max_hold` — потолок
        удержания: после него аренда не продлевается и держатель получает `LeaseLost("max_hold")`;
        `require_quota` (вместе с `for_operation`) — не выдавать аккаунты с исчерпанной квотой операции.
        """
        from identity_hub.lease import lease_scope

        return lease_scope(
            self,
            on_lost,
            on_yield_requested,
            for_operation=for_operation,
            tags=tags,
            labels=labels,
            profile=profile,
            among=among,
            exclude=exclude,
            wait=wait,
            idempotency_key=idempotency_key,
            purpose=purpose,
            ref=ref,
            max_hold=max_hold,
            require_quota=require_quota,
            keep_free=keep_free,
            priority=priority,
        )

    async def acquire(
        self,
        *,
        for_operation: str | None = None,
        tags: Iterable[str] = (),
        labels: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        # список id, подзапрос read-схемы (только PostgreSQL) или предикат от Identity (F5.1)
        among: Select[Any] | Iterable[UUID] | Callable[[Identity], bool] | None = None,
        exclude: Iterable[UUID] = (),
        wait: timedelta | float | None = None,
        idempotency_key: str | None = None,
        query: CandidateQuery | None = None,
        purpose: str | None = None,
        ref: str | None = None,
        max_hold: timedelta | None = None,
        require_quota: bool = False,
        keep_free: int | None = None,
        priority: int | None = None,
    ) -> Lease:
        """Низкоуровневый захват без heartbeat; обычный путь — `async with pool.lease(...)`.

        `query` — готовый отбор кандидатов (служебные пулы входа и проб).
        """
        from identity_hub.lease import acquire

        return await acquire(
            self,
            for_operation=for_operation,
            tags=tags,
            labels=labels,
            profile=profile,
            among=among,
            exclude=exclude,
            wait=wait,
            idempotency_key=idempotency_key,
            query=query,
            purpose=purpose,
            ref=ref,
            max_hold=max_hold,
            require_quota=require_quota,
            keep_free=keep_free,
            priority=priority,
        )

    def lease_options(
        self,
        *,
        purpose: str | None,
        ref: str | None,
        max_hold: timedelta | None = None,
        priority: int | None = None,
    ) -> LeaseOptions:
        """Свойства новой аренды: значения вызова поверх значений пула, проверенные и очищенные."""
        hold = check_max_hold(max_hold, self.timing) if max_hold is not None else self.max_hold
        if priority is not None and self.shared:
            raise ValidationFailed("priority не поддерживается для SHARED-пула")
        return LeaseOptions(
            purpose=check_purpose(purpose) if purpose is not None else self.purpose,
            ref=check_ref(ref) if ref is not None else None,
            max_hold_seconds=hold.total_seconds() if hold is not None else None,
            priority=check_priority(priority) if priority is not None else self.priority,
        )

    async def capacity(self) -> int:
        """Сколько identity пула можно выдать прямо сейчас; события `pool.capacity.low/empty`
        пишутся при смене состояния (FR-13.3, D-119)."""
        return await self._check_capacity()

    async def capacity_detail(self) -> PoolCapacity:
        """Точная свободная ёмкость и часть, удерживаемая резервом."""
        async with self.space._tx(own=True) as tx:
            query = self.candidate_query()
            free = await tx.count_candidates(query, 1_000_000)
            effective = await self._effective_reserve(tx, query, self.reserve)
        reserved = min(free, effective)
        return PoolCapacity(free=free, reserved=reserved, available=free - reserved)

    async def _effective_reserve(self, tx: Any, query: CandidateQuery, reserve: int) -> int:
        if reserve == 0 or self.reserve_strict:
            return reserve
        total = await tx.count_candidates(replace(query, shared=True), reserve + 1)
        return 0 if total <= reserve else reserve

    async def _check_capacity(self) -> int:
        """Свободная ёмкость пула; `pool.capacity.low/empty` — при смене состояния (FR-13.3, D-119)."""
        threshold = self.capacity_low or 0
        async with self.space._tx(own=True) as tx:
            query = self.candidate_query()
            free = await tx.count_candidates(query, threshold + self.reserve + 1)
            effective = await self._effective_reserve(tx, query, self.reserve)
            available = max(0, free - effective)
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
        record_metric(
            self.space.hub.metrics,
            "gauge",
            "identity_hub_pool_available",
            available,
            space=self.space.slug,
            service=self.service.name,
            pool=self.name,
        )
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


def _expand_profile_filter(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Превратить `fingerprint.browser=chrome` в JSON containment по вложенному объекту."""
    result: dict[str, Any] = {}
    for path, value in profile.items():
        target = result
        parts = path.split(".")
        for part in parts[:-1]:
            existing = target.get(part)
            if existing is None:
                nested: dict[str, Any] = {}
                target[part] = nested
                target = nested
            elif isinstance(existing, dict):
                target = existing
            else:
                raise ValidationFailed(f"фильтр профиля: конфликт пути {path!r}")
        leaf = parts[-1]
        existing = target.get(leaf)
        if isinstance(existing, dict) and not isinstance(value, Mapping):
            raise ValidationFailed(f"фильтр профиля: конфликт пути {path!r}")
        target[leaf] = value
    return result


class PoolsMixin:
    async def _revoke_lease(self: Any, identity_id: UUID, *, reason: str, actor: str) -> bool:
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
        # после коммита: аренда этого процесса узнаёт об отзыве сразу, а не на следующем heartbeat
        live = self.hub._live_leases.get(revoked.lease_id)
        if live is not None:
            live._revoked_elsewhere()
        return True

    def _pool(
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
        stats_flush: timedelta | None = None,
        purpose: str | None = None,
        max_hold: timedelta | None = None,
        reserve: int = 0,
        reserve_strict: bool = False,
        priority: int = 0,
        yield_grace: timedelta | None = None,
    ) -> Pool:
        defaults = LeaseTiming()
        timing = LeaseTiming(
            ttl=ttl or defaults.ttl,
            heartbeat=heartbeat or defaults.heartbeat,
            safety_margin=safety_margin or defaults.safety_margin,
            stats_flush=stats_flush or defaults.stats_flush,
        )
        return Pool(
            space=self,
            name=name,
            service=self.hub.service(service),
            tags=frozenset(normalize_tag(tag) for tag in tags),
            labels=dict(labels or {}),
            profile=dict(profile or {}),
            pacing=pacing,
            proxy_policy=proxy_policy,
            timing=timing,
            holder=holder or default_holder(),
            capacity_low=capacity_low,
            purpose=purpose,
            max_hold=max_hold,
            reserve=reserve,
            reserve_strict=reserve_strict,
            priority=priority,
            yield_grace=yield_grace,
        )
