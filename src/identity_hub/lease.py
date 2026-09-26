"""Аренда: эксклюзивное право воркера работать с аккаунтом (ТЗ §3.6, FR-6, G-1).

Аренда держится записью с TTL и heartbeat (FR-6.2), а не открытой транзакцией или advisory lock.
Публичный `lease_id` (UUIDv7), приватный `token` (никогда не логируется, FR-6.4), монотонный `fencing`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import random
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._space import logger
from identity_hub.context import LeaseContext, _current
from identity_hub.crypto import associated_data
from identity_hub.domain.data import validate_model
from identity_hub.domain.regions import Command
from identity_hub.enums import AuthState, LeaseEndReason, ProxyStatus, SessionStatus
from identity_hub.errors import LeaseLost, LeaseUnavailable, StaleFencing
from identity_hub.events import EventDraft
from identity_hub.service import ProxyDownPolicy
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import CandidateQuery, IdentityRow, LeaseCounters, LeaseRow, ProxyRow, StoreTx

if TYPE_CHECKING:
    from identity_hub.operation import Operation
    from identity_hub.pool import Pool

__all__ = ["Lease", "OnLost", "acquire", "lease_scope"]


OnLost = Callable[[], Any]
"""Колбэк потери аренды: синхронный или async (например, `client.disconnect`)."""


class Lease:
    """Эксклюзивная аренда аккаунта.

    Внутри `async with pool.lease()` фоновая задача продлевает аренду каждые `heartbeat`
    на отдельной транзакции (T-2). Если продлить не удалось за `TTL − safety_margin`
    (по `time.monotonic()`), аренда считается потерянной раньше, чем её сможет взять другой
    воркер (G-1.2): вызывается `on_lost`, блок прерывается, наружу выходит `LeaseLost` (D-112).
    """

    def __init__(
        self, pool: Pool, row: LeaseRow, identity: IdentityRow, proxy: ProxyRow | None = None
    ) -> None:
        self.pool = pool
        self.proxy_info = proxy
        self._row = row
        self._token = row.token  # приватный: только для продления и освобождения
        self.identity = identity
        self._counters = LeaseCounters.empty()
        self._lost: str | None = None
        self._closed = False
        self._last_ok = pool.space.hub.clock.monotonic()
        self._owner: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._on_lost: OnLost | None = None
        self._cancelled_by_us = False
        self._released = False
        self._on_lost_fired = False

    # --------------------------------------------------------- свойства

    @property
    def lease_id(self) -> UUID:
        return self._row.lease_id

    @property
    def identity_id(self) -> UUID:
        return self._row.identity_id

    @property
    def fencing(self) -> int:
        return self._row.fencing

    @property
    def expires_at(self) -> datetime:
        return self._row.expires_at

    @property
    def acquired_at(self) -> datetime:
        return self._row.acquired_at

    @property
    def service(self) -> str:
        return self.pool.service.name

    @property
    def holder(self) -> str:
        return self._row.holder

    @property
    def proxy_id(self) -> UUID | None:
        return self._row.proxy_id

    @property
    def proxy(self) -> str | None:
        """URL прокси аренды с логином и паролем (для клиента платформы; не логировать)."""
        if self.proxy_info is None:
            return None
        return self.pool.space.proxy_url_of(self.proxy_info)  # type: ignore[attr-defined, no-any-return]

    @property
    def lost(self) -> bool:
        return self._lost is not None or self._deadline_passed()

    @property
    def context(self) -> LeaseContext:
        return LeaseContext(self.lease_id, self.identity_id, self.service, self.pool.name, self.holder)

    def check(self) -> None:
        """Бросить LeaseLost, если аренда потеряна (для участков без await)."""
        if self._deadline_passed() and self._lost is None:
            self._lost = "heartbeat"
        if self._lost is not None:
            raise LeaseLost(self.lease_id, self._lost)

    def __repr__(self) -> str:
        return (
            f"Lease(lease_id={self.lease_id}, identity_id={self.identity_id}, "
            f"pool={self.pool.name!r}, fencing={self.fencing})"
        )

    # --------------------------------------------------------- heartbeat и самоограждение

    def _deadline(self) -> float:
        timing = self.pool.timing
        return self._last_ok + (timing.ttl - timing.safety_margin).total_seconds()

    def _deadline_passed(self) -> bool:
        return self.pool.space.hub.clock.monotonic() >= self._deadline()

    def _start(self, on_lost: OnLost | None) -> None:
        self._on_lost = on_lost
        self._owner = asyncio.current_task()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"ih-heartbeat-{self.lease_id}"
        )

    async def _heartbeat_loop(self) -> None:
        clock = self.pool.space.hub.clock
        interval = self.pool.timing.heartbeat.total_seconds()
        while not self._closed:
            await asyncio.sleep(max(0.0, min(interval, self._deadline() - clock.monotonic())))
            if self._closed:
                return
            sent_at = clock.monotonic()
            confirmed = False
            try:
                budget = max(0.001, self._deadline() - sent_at)
                expires_at = await asyncio.wait_for(self._extend(), timeout=budget)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("heartbeat аренды %s не прошёл", self.lease_id, exc_info=True)
            else:
                if expires_at is None:
                    await self._mark_lost("revoked")  # аренды с нашим token больше нет
                    return
                confirmed = True
                self._last_ok = sent_at
                self._row = replace(self._row, expires_at=expires_at)
            if not confirmed and self._deadline_passed():
                await self._mark_lost("heartbeat")
                return

    async def _extend(self) -> datetime | None:
        counters, self._counters = self._counters, LeaseCounters.empty()
        try:
            async with self.pool.space._tx(own=True) as tx:
                return await tx.extend_lease(
                    self._row, self.pool.timing.ttl.total_seconds(), counters, shared=self.pool.shared
                )
        except BaseException:
            self._merge_back(counters)
            raise

    def _merge_back(self, counters: LeaseCounters) -> None:
        for kind, n in counters.operations.items():
            self._counters.operations[kind] = self._counters.operations.get(kind, 0) + n
        self._counters.succeeded += counters.succeeded
        self._counters.failed += counters.failed
        self._counters.last_error = self._counters.last_error or counters.last_error

    async def _mark_lost(self, reason: str) -> None:
        if self._lost is None:
            self._lost = reason
            logger.warning("аренда %s потеряна: %s", self.lease_id, reason)
        await self._fire_on_lost()
        if self._owner is not None and not self._owner.done():
            self._cancelled_by_us = True
            self._owner.cancel(f"identity_hub: аренда {self.lease_id} потеряна")

    async def _fire_on_lost(self) -> None:
        """Вызвать on_lost ровно один раз, каким бы путём ни обнаружилась потеря (heartbeat или check())."""
        if self._on_lost_fired or self._on_lost is None:
            return
        self._on_lost_fired = True
        try:
            result = self._on_lost()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("on_lost аренды %s завершился ошибкой", self.lease_id)

    async def _stop_heartbeat(self) -> None:
        self._closed = True
        task = self._heartbeat_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --------------------------------------------------------- счётчики (P4.2)

    def _count(self, kind: str, *, ok: bool, error: str | None = None) -> None:
        self._counters.operations[kind] = self._counters.operations.get(kind, 0) + 1
        if ok:
            self._counters.succeeded += 1
        else:
            self._counters.failed += 1
            self._counters.last_error = error

    # --------------------------------------------------------- операции (FR-7)

    def operation(
        self, kind: str, *, key: str | None = None
    ) -> contextlib.AbstractAsyncContextManager[Operation]:
        """`async with lease.operation("send") as op:` — проверка, квота до вызова платформы, результат."""
        from identity_hub.operation import operation_scope

        return operation_scope(self, kind, key)

    # --------------------------------------------------------- секреты и сессии (FR-5.2, 5.3, 5.6)

    async def credentials(self) -> BaseModel | None:
        """Расшифрованные credentials — только под арендой (FR-5.2)."""
        self.check()
        async with self.pool.space._tx(own=True) as tx:
            payload = await tx.get_credentials(self.identity_id)
        if payload is None:
            return None
        aad = associated_data("ih_credentials", self.identity_id)
        return self.pool.space.hub.envelope.open_model(self.pool.service.credentials, payload, aad)

    async def session(self, slot: str = DEFAULT_SLOT) -> BaseModel | None:
        """Расшифрованная действующая сессия слота или None."""
        self.check()
        async with self.pool.space._tx(own=True) as tx:
            found = await tx.get_session(self.identity_id, slot)
        if found is None or found[1] != SessionStatus.VALID:
            return None
        aad = associated_data("ih_sessions", self.identity_id, slot)
        return self.pool.space.hub.envelope.open_model(self.pool.service.session, found[0], aad)

    async def save_session(
        self,
        session: BaseModel | Mapping[str, Any],
        slot: str = DEFAULT_SLOT,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        """Сохранить результат входа; запись устаревшей аренды — StaleFencing (FR-5.3, G-1.4)."""
        self.check()
        model = validate_model(self.pool.service.session, session, "session")
        aad = associated_data("ih_sessions", self.identity_id, slot)
        payload = self.pool.space.hub.envelope.seal_model(model, aad)  # до открытия транзакции (T-5)
        async with self.pool.space._tx(own=True) as tx:
            await self._check_fencing(tx)
            if not await tx.put_session(
                self.identity_id, slot, payload, fencing=self.fencing, expires_at=expires_at
            ):
                raise StaleFencing(self.identity_id, self.fencing)
            await self.pool.space._emit(
                tx,
                EventDraft(
                    "session.saved",
                    actor=self.holder,
                    service=self.service,
                    identity_id=self.identity_id,
                    lease_id=self.lease_id,
                    data={"slot": slot},
                ),
            )

    async def mark_authenticated(self, slot: str = DEFAULT_SLOT) -> None:
        """Вход выполнен пользователем сам (без Authenticator, FR-5.6): auth → authenticated."""
        self.check()
        async with self.pool.space._tx(own=True) as tx:
            await self._check_fencing(tx)
            await apply_region_change(
                self.pool.space,
                tx,
                self.identity_id,
                "auth",
                slot,
                Command(AuthState.AUTHENTICATED, actor=self.holder),
                lease_id=self.lease_id,
            )

    async def _check_fencing(self, tx: StoreTx) -> None:
        current = await tx.current_fencing(self.identity_id, lock=True)
        if current != self.fencing:
            raise StaleFencing(self.identity_id, self.fencing, current)

    # --------------------------------------------------------- освобождение

    async def release(
        self, reason: LeaseEndReason | str = LeaseEndReason.RELEASED, error: str | None = None
    ) -> None:
        """Освободить аренду (повтор — no-op). Обычно вызывается выходом из `async with pool.lease()`."""
        await self._stop_heartbeat()
        if self._released:
            return
        self._released = True
        counters, self._counters = self._counters, LeaseCounters.empty()
        if error is not None:
            counters.last_error = error[:2000]
        event_type = {
            LeaseEndReason.RELEASED: "lease.released",
            LeaseEndReason.FAILED: "lease.failed",
            LeaseEndReason.LOST: "lease.lost",
        }.get(LeaseEndReason(reason), "lease.released")
        try:
            async with self.pool.space._tx(own=True) as tx:
                closed = await tx.release_lease(self._row, str(reason), counters, shared=self.pool.shared)
                if closed:
                    await self.pool.space._emit(
                        tx,
                        EventDraft(
                            event_type,
                            actor=self.holder,
                            service=self.service,
                            identity_id=self.identity_id,
                            lease_id=self.lease_id,
                            data={
                                "pool": self.pool.name,
                                "reason": str(reason),
                                "error": counters.last_error,
                            },
                        ),
                    )
        except Exception:
            logger.warning("не удалось освободить аренду %s", self.lease_id, exc_info=True)


@contextlib.asynccontextmanager
async def lease_scope(pool: Pool, on_lost: OnLost | None, **kwargs: Any) -> AsyncIterator[Lease]:
    """Тело `pool.lease()`: захват → heartbeat и контекст логов → освобождение с причиной."""
    lease = await acquire(pool, **kwargs)
    token = _current.set(lease.context)
    lease._start(on_lost)
    try:
        yield lease
    except asyncio.CancelledError as exc:
        if not lease._cancelled_by_us:
            await lease.release(LeaseEndReason.FAILED, "cancelled")
            raise
        owner = asyncio.current_task()
        if owner is not None:
            owner.uncancel()
        await lease.release(LeaseEndReason.LOST, f"lost: {lease._lost}")
        raise LeaseLost(lease.lease_id, lease._lost or "heartbeat") from exc
    except LeaseLost:
        await lease._fire_on_lost()
        await lease.release(LeaseEndReason.LOST, f"lost: {lease._lost or 'heartbeat'}")
        raise
    except BaseException as exc:
        await lease.release(LeaseEndReason.FAILED, f"{type(exc).__name__}: {exc}")
        raise
    else:
        if lease._lost is not None:
            await lease._fire_on_lost()
            await lease.release(LeaseEndReason.LOST, f"lost: {lease._lost}")
            raise LeaseLost(lease.lease_id, lease._lost)
        await lease.release(LeaseEndReason.RELEASED)
    finally:
        _current.reset(token)


class _PoolExhausted(Exception):
    """В пуле прокси нет живого прокси с запасом ёмкости."""


class _SkipCandidate(Exception):
    def __init__(self, identity_id: UUID) -> None:
        self.identity_id = identity_id


async def acquire(
    pool: Pool,
    *,
    for_operation: str | None = None,
    tags: Iterable[str] = (),
    labels: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    among: Any = None,
    exclude: Iterable[UUID] = (),
    wait: timedelta | float | None = None,
    idempotency_key: str | None = None,
    query: CandidateQuery | None = None,
) -> Lease:
    """Выдать аккаунт в аренду (FR-6.1). Короткая транзакция: выбор SKIP LOCKED + прокси + аренда + история.

    `wait` — ждать освобождения подходящего аккаунта; по истечении — `LeaseUnavailable`.
    Повтор с тем же `idempotency_key`, пока аренда жива, возвращает ту же аренду (FR-6.5).
    """
    if query is None:  # готовый запрос — внутренние выборки библиотеки (вход, сверка)
        query = pool.candidate_query(
            for_operation=for_operation,
            tags=tags,
            labels=labels,
            profile=profile,
            among=among,
            exclude=exclude,
        )
    space = pool.space
    clock = space.hub.clock
    wait_seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait or 0)
    deadline = clock.monotonic() + wait_seconds
    delay = 0.05
    while True:
        try:
            async with space._tx(own=True) as tx:
                if idempotency_key is not None:
                    existing = await tx.find_live_lease(idempotency_key)
                    if existing is not None:
                        identity = await space._identity(tx, existing.identity_id)
                        policy = pool.proxy_policy
                        proxy = (
                            await tx.get_binding(existing.identity_id) if policy and policy.sticky else None
                        )
                        if proxy is not None:
                            existing = replace(existing, proxy_id=proxy.id)
                        return Lease(pool, existing, identity, proxy)
                candidate = await tx.pick_candidate(query)
                if candidate is not None:
                    proxy, drafts = await _resolve_proxy(pool, tx, candidate)
                    row, reclaimed = await tx.acquire_lease(
                        candidate,
                        pool=pool.name,
                        holder=pool.holder,
                        ttl_seconds=pool.timing.ttl.total_seconds(),
                        idempotency_key=idempotency_key,
                        shared=pool.shared,
                        proxy_id=proxy.id if proxy else None,
                    )
                    if reclaimed is not None:
                        drafts.append(
                            _lease_event(
                                "lease.expired", pool, reclaimed, {"expired_at": reclaimed.expires_at}
                            )
                        )
                    drafts.append(_lease_event("lease.acquired", pool, row, {"shared": pool.shared}))
                    await space._emit(tx, *drafts)
                    return Lease(pool, row, candidate, proxy)
        except _SkipCandidate as skip:
            query = replace(query, exclude=query.exclude | {skip.identity_id})
            continue
        except _PoolExhausted:
            await _pool_exhausted(pool)
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise LeaseUnavailable(f"пул {pool.name}: нет свободного аккаунта")
        await asyncio.sleep(min(remaining, delay * (0.5 + random.random())))
        delay = min(delay * 2, 2.0)


async def _resolve_proxy(
    pool: Pool, tx: StoreTx, candidate: IdentityRow
) -> tuple[ProxyRow | None, list[EventDraft]]:
    """Прокси аренды по политике сервиса/пула (FR-8.2, 8.3). В той же транзакции, что и захват."""
    policy = pool.proxy_policy
    if policy is None:
        return None, []
    current = await tx.get_binding(candidate.id) if policy.sticky else None
    if current is not None and current.status == ProxyStatus.ALIVE:
        return current, []
    if current is not None and policy.on_proxy_down is ProxyDownPolicy.WAIT:
        raise _SkipCandidate(candidate.id)  # прокси упал между выборкой и захватом
    chosen = await tx.choose_proxy(policy.pool, policy.max_identities, claim=policy.sticky)
    if chosen is None:
        raise _PoolExhausted
    drafts: list[EventDraft] = []
    if policy.sticky:
        await tx.bind_proxy(candidate.id, chosen.id, current.id if current else None)
        drafts.append(
            EventDraft(
                "proxy.binding.changed",
                actor=pool.holder,
                service=pool.service.name,
                identity_id=candidate.id,
                proxy_id=chosen.id,
                data={
                    "from_proxy_id": str(current.id) if current else None,
                    "to_proxy_id": str(chosen.id),
                    "reason": "rebind" if current else "initial",
                    "pool": policy.pool,
                },
            )
        )
    return chosen, drafts


async def _pool_exhausted(pool: Pool) -> None:
    policy = pool.proxy_policy
    assert policy is not None
    try:
        async with pool.space._tx(own=True) as tx:
            await pool.space._emit(
                tx,
                EventDraft(
                    "proxy.pool.exhausted",
                    actor=pool.holder,
                    service=pool.service.name,
                    data={"proxy_pool": policy.pool, "pool": pool.name},
                ),
            )
    except Exception:
        logger.warning("не удалось записать proxy.pool.exhausted", exc_info=True)


def _lease_event(event_type: str, pool: Pool, row: LeaseRow, extra: Mapping[str, Any]) -> EventDraft:
    return EventDraft(
        event_type,
        actor=row.holder,
        service=pool.service.name,
        identity_id=row.identity_id,
        lease_id=row.lease_id,
        data={"pool": row.pool, "holder": row.holder, "fencing": row.fencing, **extra},
    )
