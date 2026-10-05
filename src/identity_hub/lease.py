"""Аренда: эксклюзивное право воркера работать с аккаунтом (ТЗ §3.6, FR-6, G-1).

Аренда держится записью с TTL и heartbeat (FR-6.2), а не открытой транзакцией или advisory lock.
Публичный `lease_id` (UUIDv7), приватный `token` (никогда не логируется, FR-6.4), монотонный `fencing`.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import random
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._space import logger
from identity_hub.context import LeaseContext, _current
from identity_hub.crypto import SecretValue, associated_data, credential_associated_data
from identity_hub.domain.credentials import ACTIVE, is_expired, session_event_data, session_mode
from identity_hub.domain.data import validate_model
from identity_hub.domain.idempotency import LEASE_FAMILY, fingerprint, namespace
from identity_hub.domain.redaction import sanitize_text
from identity_hub.domain.regions import Command
from identity_hub.enums import (
    AuthState,
    LeaseEndReason,
    LeaseUnavailableReason,
    ProxyStatus,
    SessionStatus,
)
from identity_hub.errors import (
    IdempotencyConflict,
    LeaseExpired,
    LeaseLost,
    LeaseRevoked,
    LeaseUnavailable,
    StaleFencing,
    ValidationFailed,
    describe_error,
)
from identity_hub.events import EventDraft
from identity_hub.metrics import record_metric
from identity_hub.service import ProxyDownPolicy
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import (
    CandidateQuery,
    IdempotencyRequest,
    IdentityRow,
    LeaseCounters,
    LeaseFence,
    LeaseRenewal,
    LeaseRow,
    ProxyRow,
    StoreTx,
)

if TYPE_CHECKING:
    from identity_hub.operation import Operation
    from identity_hub.pool import Pool
    from identity_hub.verifications import Verification

__all__ = ["Lease", "OnLost", "acquire", "lease_scope"]


OnLost = Callable[[], Any]
"""Колбэк потери аренды: синхронный или async (например, `client.disconnect`)."""

HEARTBEAT_RETRY_BASE = 0.5
"""Пауза перед первым повтором неудачного heartbeat, секунды; дальше удваивается."""
HEARTBEAT_MIN_BUDGET = 1.0
"""Время, которое повтор heartbeat оставляет самой попытке, если дедлайн это позволяет."""
RELEASE_TIMEOUT = 10.0
"""Сколько освобождение аренды ждёт записи в БД, даже если владеющую задачу отменяют."""
PROXY_EXHAUSTED_INTERVAL = 60.0
"""Не чаще одного события `proxy.pool.exhausted` на пул в процессе за этот интервал, секунды."""


def _end_reason(reason: LeaseEndReason | str) -> LeaseEndReason:
    try:
        return LeaseEndReason(reason)
    except ValueError:
        allowed = ", ".join(item.value for item in LeaseEndReason)
        raise ValidationFailed(f"причина завершения аренды — одно из: {allowed}") from None


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
        self._revoked_on_release = False
        self._revoke_task: asyncio.Task[None] | None = None  # отзыв в этом же процессе (D-213)
        self._closed = False
        self._last_ok = pool.space.hub.clock.monotonic()
        self._last_flush = self._last_ok  # последняя запись времени и счётчиков в историю (D-210)
        self._started_at = self._last_ok
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
    def proxy(self) -> SecretValue | None:
        """URL прокси аренды с логином и паролем: строка для клиента платформы — `lease.proxy.reveal()`."""
        url = self._proxy_url()
        return SecretValue(url) if url is not None else None

    def _proxy_url(self) -> str | None:
        self.check()
        if self.proxy_info is None:
            return None
        return self.pool.space._proxy_url_of(self.proxy_info)  # type: ignore[attr-defined, no-any-return]

    @property
    def lost(self) -> bool:
        return self._lost is not None or self._deadline_passed()

    @property
    def context(self) -> LeaseContext:
        return LeaseContext(self.lease_id, self.identity_id, self.service, self.pool.name, self.holder)

    def check(self) -> None:
        """Бросить LeaseLost, если аренда потеряна или уже освобождена (для участков без await)."""
        if self._closed:  # release() вызван: работать под арендой нельзя, даже если запись не прошла
            raise LeaseLost(self.lease_id, self._lost or "released")
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
        self.pool.space.hub._live_leases[self.lease_id] = self  # отзыв в этом процессе будит heartbeat
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name=f"ih-heartbeat-{self.lease_id}"
        )

    async def _heartbeat_loop(self) -> None:
        clock = self.pool.space.hub.clock
        interval = self.pool.timing.heartbeat.total_seconds()
        failures = 0
        while not self._closed:
            remaining = self._deadline() - clock.monotonic()
            if failures == 0:
                # от последнего подтверждения, а не от запуска задачи: под FakeClock задача может
                # впервые получить ход уже после сдвига времени
                pause = min(self._last_ok + interval - clock.monotonic(), remaining)
            else:
                # После сбоя не ждём полный интервал: следующая попытка пришлась бы на сам дедлайн
                # и один короткий сбой БД отменял бы все аренды воркера (R1-03).
                backoff = min(interval, HEARTBEAT_RETRY_BASE * 2 ** (failures - 1))
                pause = min(backoff, remaining - min(HEARTBEAT_MIN_BUDGET, remaining / 2))
            await clock.idle(max(0.0, pause))  # под FakeClock будит advance(), а не реальное время
            if self._closed:
                return
            sent_at = clock.monotonic()
            confirmed = False
            try:
                # одна попытка не забирает весь остаток: зависший запрос оставляет время на повтор
                budget = min(self._deadline() - sent_at, max(interval, HEARTBEAT_MIN_BUDGET))
                renewal = await asyncio.wait_for(self._extend(), timeout=max(0.001, budget))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # без traceback и текста: в ошибке БД бывают параметры запроса с token аренды (S-8)
                logger.warning("heartbeat аренды %s не прошёл: %s", self.lease_id, describe_error(exc))
                failures += 1
            else:
                if renewal.expires_at is None:  # аренды с нашим token больше нет
                    await self._mark_lost(_lost_reason(renewal.end_reason))
                    return
                confirmed = True
                failures = 0
                self._last_ok = sent_at
                self._row = replace(self._row, expires_at=renewal.expires_at)
            if not confirmed and self._deadline_passed():
                await self._mark_lost("heartbeat")
                return

    def _revoked_elsewhere(self) -> None:
        """Аренду отозвали в этом же процессе: потеря сразу, как если бы её нашёл heartbeat (D-213)."""
        if asyncio.current_task() is self._owner:
            return  # отзыв изнутри своего же блока: владелец знает о нём, потерю найдёт heartbeat
        if self._lost is None and not self._closed:
            self._revoke_task = asyncio.create_task(
                self._mark_lost("revoked"), name=f"ih-revoked-{self.lease_id}"
            )

    async def _flush_logs(self, *, final: bool = False) -> None:
        """Пачка логов аренды в ih_lease_logs (FR-12.2). Ошибка записи не влияет на аренду."""
        sink = self.pool.space.hub.lease_log_sink
        entries = sink.drain(self.lease_id, final=final)
        if not entries:
            return
        try:
            async with self.pool.space._tx(own=True) as tx:
                await tx.insert_lease_logs(
                    [
                        {
                            "lease_id": e.lease_id,
                            "identity_id": e.identity_id,
                            "ts": e.ts,
                            "level": e.level,
                            "message": e.message,
                            "fields": e.fields,
                        }
                        for e in entries
                    ]
                )
        except Exception:
            logging.getLogger("identity_hub.sink").debug("sink ih_lease_logs недоступен", exc_info=True)

    async def _extend(self) -> LeaseRenewal:
        await self._flush_logs()
        clock = self.pool.space.hub.clock
        started = clock.monotonic()
        # история и счётчики — раз в stats_flush: между ними heartbeat меняет только ih_leases (HOT)
        flush = self.pool.shared or started - self._last_flush >= self.pool.timing.stats_flush.total_seconds()
        if not flush:
            # продление без истории — пачкой вместе с другими арендами процесса (D-211)
            return await self.pool.space.hub._heartbeats.renew(
                self._row.token, self.pool.timing.ttl.total_seconds()
            )
        counters, self._counters = self._counters, LeaseCounters.empty()
        try:
            async with self.pool.space._tx(own=True) as tx:
                renewal = await tx.extend_lease(
                    self._row,
                    self.pool.timing.ttl.total_seconds(),
                    counters,
                    shared=self.pool.shared,
                    flush=flush,
                )
        except BaseException:
            self._merge_back(counters)
            raise
        if renewal.expires_at is not None:
            self._last_flush = started
        return renewal

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
        if self._on_lost_fired:
            return
        self._on_lost_fired = True
        self.pool.space.hub._record_lease_loss(self.pool.space.slug, self.holder)
        if self._on_lost is None:
            return
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

    def verification(self, factor: str) -> Verification:
        """Фактор верификации под арендой (FR-10): start/submit/reconcile/…."""
        from identity_hub.verifications import Verification

        return Verification(self, factor)

    # --------------------------------------------------------- секреты и сессии (FR-5.2, 5.3, 5.6)

    async def credentials(self) -> BaseModel | None:
        """Расшифрованная запись credentials `default` — только под арендой (FR-5.2, D-216)."""
        return await self.credential()

    async def credential(self, kind: str = "default", name: str = "default") -> BaseModel | None:
        """Запись credentials по виду и имени под полным fence (IMP-09, D-216).

        `None` — записи нет, она выключена, отозвана или её срок вышел.
        """
        self.check()
        schema = self.pool.service.credential_schema(kind)
        async with self.pool.space._tx(own=True) as tx:
            await self._check_ownership(tx)
            found = await tx.get_credential(self.identity_id, kind, name)
            now = await tx.now()
        if (
            found is None
            or found[1].lifecycle != ACTIVE
            or is_expired(found[1].lifecycle, found[1].expires_at, now)
        ):
            return None
        aad = credential_associated_data(self.identity_id, kind, name)
        return self.pool.space.hub._envelope.open_model(schema, found[0], aad)

    async def active_credentials(self) -> dict[tuple[str, str], BaseModel]:
        """Все активные записи credentials с непрошедшим сроком: `(kind, name) → модель` (D-216)."""
        self.check()
        async with self.pool.space._tx(own=True) as tx:
            await self._check_ownership(tx)
            rows = await tx.list_credentials(self.identity_id)
            now = await tx.now()
            usable = [
                row
                for row in rows
                if row.lifecycle == ACTIVE and not is_expired(row.lifecycle, row.expires_at, now)
            ]
            payloads = [await tx.get_credential(self.identity_id, row.kind, row.name) for row in usable]
        envelope = self.pool.space.hub._envelope
        result: dict[tuple[str, str], BaseModel] = {}
        for row, found in zip(usable, payloads, strict=True):
            if found is None:
                continue
            schema = self.pool.service.credential_schema(row.kind)
            aad = credential_associated_data(self.identity_id, row.kind, row.name)
            result[(row.kind, row.name)] = envelope.open_model(schema, found[0], aad)
        return result

    async def session(self, slot: str = DEFAULT_SLOT) -> BaseModel | None:
        """Расшифрованная действующая сессия слота или None."""
        return await self._stored_session(slot, valid_only=True)

    async def _stored_session(self, slot: str, *, valid_only: bool) -> BaseModel | None:
        """Сессия слота; `valid_only=False` — и признанная недействительной (для повторного входа)."""
        self.check()
        async with self.pool.space._tx(own=True) as tx:
            await self._check_ownership(tx)
            found = await tx.get_session(self.identity_id, slot)
        if found is None or (valid_only and found[1] != SessionStatus.VALID):
            return None
        aad = associated_data("ih_sessions", self.identity_id, slot)
        return self.pool.space.hub._envelope.open_model(self.pool.service.session, found[0], aad)

    async def save_session(
        self,
        session: BaseModel | Mapping[str, Any],
        slot: str = DEFAULT_SLOT,
        *,
        expires_at: datetime | None = None,
        mode: str = "in_place",
    ) -> None:
        """Сохранить сессию слота; запись устаревшей аренды — StaleFencing (FR-5.3, G-1.4).

        `mode="in_place"` обновляет текущую запись слота, `"replace"` создаёт новую и отзывает
        прежнюю — она остаётся в истории (IMP-10, D-219).
        """
        self.check()
        mode = session_mode(mode)
        model = validate_model(self.pool.service.session, session, "session")
        aad = associated_data("ih_sessions", self.identity_id, slot)
        payload = self.pool.space.hub._envelope.seal_model(model, aad)  # до открытия транзакции (T-5)
        async with self.pool.space._tx(own=True) as tx:
            await self._check_fencing(tx)
            written = await tx.put_session(
                self.identity_id, slot, payload, fencing=self.fencing, expires_at=expires_at, mode=mode
            )
            if written is None:
                raise StaleFencing(self.identity_id, self.fencing)
            await self.pool.space._emit(
                tx,
                EventDraft(
                    "session.saved",
                    actor=self.holder,
                    service=self.service,
                    identity_id=self.identity_id,
                    lease_id=self.lease_id,
                    data=session_event_data(slot, mode, written),
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

    @property
    def fence(self) -> LeaseFence:
        """Кому принадлежит аренда: запись от её имени принимается при полном совпадении (IMP-05)."""
        row = self._row
        return LeaseFence(row.identity_id, row.lease_id, row.token, row.fencing, shared=self.pool.shared)

    async def _check_ownership(self, tx: StoreTx) -> None:
        """Чтение секрета: аренда жива и наша (R2-S05), без блокировки строки."""
        await tx.check_fence(self.fence, lock=False)

    async def _check_fencing(self, tx: StoreTx) -> None:
        """Запись от имени аренды: полный fence под блокировкой identity (IMP-05, D-212)."""
        await tx.check_fence(self.fence)

    # --------------------------------------------------------- освобождение

    async def release(
        self, reason: LeaseEndReason | str = LeaseEndReason.RELEASED, error: str | None = None
    ) -> None:
        """Освободить аренду. Обычно вызывается выходом из `async with pool.lease()`.

        `reason` — `LeaseEndReason` или его строковое значение; другая строка — `ValidationFailed`,
        аренда при этом не тронута. Запись идёт под `asyncio.shield` с таймаутом
        `RELEASE_TIMEOUT`: отмена вызывающей задачи (в том числе повторная) не прерывает её и
        пробрасывается после завершения. Если запись не удалась, в лог пишется предупреждение,
        а повторный вызов пробует снова; после успеха повтор — no-op.
        """
        await self._release_quietly(reason, error)
        if self._revoked_on_release:
            # освобождение отозванной аренды: воркер должен узнать, что его сняли (IMP-06, D-213)
            self._revoked_on_release = False
            raise LeaseRevoked(self.lease_id, "revoked")

    async def _release_quietly(self, reason: LeaseEndReason | str, error: str | None = None) -> None:
        """Освобождение без `LeaseRevoked`: выход из `async with` сообщает потерю своей веткой."""
        end = _end_reason(reason)
        self._closed = True  # с этого момента check() бросает LeaseLost
        if self._released:
            return
        task = asyncio.ensure_future(self._release(end, error))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():  # запись ограничена RELEASE_TIMEOUT, ждём её при любой отмене
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            raise

    async def _release(self, end: LeaseEndReason, error: str | None) -> None:
        await self._stop_heartbeat()
        if self._released:
            return
        await self._flush_logs(final=True)
        counters, self._counters = self._counters, LeaseCounters.empty()
        if error is not None:
            counters.last_error = sanitize_text(error)
        event_type = {
            LeaseEndReason.RELEASED: "lease.released",
            LeaseEndReason.FAILED: "lease.failed",
            LeaseEndReason.LOST: "lease.lost",
        }.get(end, "lease.released")
        try:
            async with asyncio.timeout(RELEASE_TIMEOUT), self.pool.space._tx(own=True) as tx:
                outcome = await tx.release_lease(self._row, str(end), counters, shared=self.pool.shared)
                if outcome == "expired":
                    event_type, end = "lease.expired", LeaseEndReason.EXPIRED
                if outcome in ("released", "expired"):
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
                                "reason": str(end),
                                "error": counters.last_error,
                            },
                        ),
                    )
        except Exception as exc:
            self._merge_back(counters)  # повторный release() допишет счётчики
            logger.warning("не удалось освободить аренду %s: %s", self.lease_id, describe_error(exc))
            return
        self._released = True  # только после успешной записи: иначе повтор был бы no-op (R1-14)
        self.pool.space.hub._live_leases.pop(self.lease_id, None)
        if outcome == "revoked":
            self._revoked_on_release = True
        hub = self.pool.space.hub
        record_metric(
            hub.metrics,
            "histogram",
            "identity_hub_lease_duration_seconds",
            max(0.0, hub.clock.monotonic() - self._started_at),
            space=self.pool.space.slug,
            service=self.service,
            pool=self.pool.name,
            reason=str(end),
        )
        if end is not LeaseEndReason.RELEASED:
            record_metric(
                hub.metrics,
                "counter",
                "identity_hub_failures_total",
                space=self.pool.space.slug,
                service=self.service,
                scope="lease",
                reason=str(end),
            )


def _lost_reason(end_reason: LeaseEndReason | None) -> str:
    """Причина потери для держателя по ответу продления.

    Строки с нашим token нет, а история открыта или неизвестна (`None`) — аренду сняли: это
    прежнее «revoked». Истечение срока и закрытие с другой причиной называются своими именами.
    """
    return str(end_reason or LeaseEndReason.REVOKED)


def _lost_error(reason: str | None) -> type[LeaseLost]:
    if reason == LeaseEndReason.REVOKED:
        return LeaseRevoked
    return LeaseExpired if reason == LeaseEndReason.EXPIRED else LeaseLost


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
            await lease._release_quietly(LeaseEndReason.FAILED, "cancelled")
            raise
        owner = asyncio.current_task()
        if owner is not None:
            owner.uncancel()
        await lease._release_quietly(LeaseEndReason.LOST, f"lost: {lease._lost}")
        raise _lost_error(lease._lost)(lease.lease_id, lease._lost or "heartbeat") from exc
    except LeaseLost:
        await lease._fire_on_lost()
        await lease._release_quietly(LeaseEndReason.LOST, f"lost: {lease._lost or 'heartbeat'}")
        raise
    except BaseException as exc:
        # только имя типа: текст исключения пользователя бывает с URL, ключами и паролем прокси (S-8)
        await lease._release_quietly(LeaseEndReason.FAILED, type(exc).__name__)
        raise
    else:
        if lease._lost is not None:
            await lease._fire_on_lost()
            await lease._release_quietly(LeaseEndReason.LOST, f"lost: {lease._lost}")
            # R1-16: тело завершилось успешно, но аренду потеряли по пути — признак для вызывающего
            raise _lost_error(lease._lost)(lease.lease_id, lease._lost, body_completed=True)
        await lease._release_quietly(LeaseEndReason.RELEASED)
    finally:
        _current.reset(token)


class _PoolExhausted(Exception):
    """В пуле прокси нет живого прокси с запасом ёмкости."""


class _SkipCandidate(Exception):
    def __init__(self, identity_id: UUID) -> None:
        self.identity_id = identity_id


async def _matching_ids(pool: Pool, predicate: Callable[[Any], bool]) -> list[UUID]:
    """Id аккаунтов сервиса пула, для которых `predicate(identity)` истинен; читается постранично."""
    from identity_hub.api.identities import IdentityFilter

    matched: list[UUID] = []
    after: str | None = None
    while True:
        page = await pool.space.identities.list(  # type: ignore[attr-defined]
            IdentityFilter(service=pool.service.name), limit=500, after=after
        )
        matched.extend(identity.id for identity in page.items if predicate(identity))
        if page.next_cursor is None:
            return matched
        after = page.next_cursor


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
    Повтор с тем же `idempotency_key`, пока аренда жива, возвращает ту же аренду (FR-6.5) — только
    тому же пулу, сервису и holder; иначе `IdempotencyConflict`. Ключ действует в пределах пространства.
    В shared-режиме ключ не поддерживается: живые shared-аренды не хранятся (D-140).
    """
    if idempotency_key is not None and pool.shared:
        raise ValidationFailed("idempotency_key не поддерживается для shared-аренды")
    explicit_query = query
    if callable(among):  # предикат от Identity: заранее превращается в список id (F5.1)
        among = await _matching_ids(pool, among)
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
    started = clock.monotonic()
    wait_seconds = wait.total_seconds() if isinstance(wait, timedelta) else float(wait or 0)
    deadline = clock.monotonic() + wait_seconds
    delay = 0.05
    exhausted_reported = False
    requested = query  # без кандидатов, пропущенных из-за гонки: они заняты, а не недопустимы
    while True:
        try:
            async with space._tx(own=True) as tx:
                if idempotency_key is not None:
                    # Ключ принадлежит одной команде (пул + сервис + holder) и блокируется первым:
                    # параллельный повтор ждёт здесь и затем получает уже выданную аренду.
                    key_namespace = namespace(tx.space.id, LEASE_FAMILY)
                    await tx.claim_idempotency_key(
                        IdempotencyRequest(
                            namespace=key_namespace,
                            key=idempotency_key,
                            operation=LEASE_FAMILY,
                            fingerprint=fingerprint(
                                LEASE_FAMILY,
                                {"pool": pool.name, "service": pool.service.name, "holder": pool.holder},
                            ),
                            ttl=space.hub.retention.operation_keys,
                        )
                    )
                    existing = await tx.find_live_lease(idempotency_key)
                    if existing is not None:
                        identity = await space._identity(tx, existing.identity_id)
                        # запись ключа могла истечь раньше аренды: живую аренду сверяем напрямую
                        if (
                            existing.pool != pool.name
                            or existing.holder != pool.holder
                            or identity.service != pool.service.name
                        ):
                            raise IdempotencyConflict(LEASE_FAMILY)
                        policy = pool.proxy_policy
                        proxy = (
                            await tx.get_binding(existing.identity_id) if policy and policy.sticky else None
                        )
                        if proxy is not None:
                            existing = replace(existing, proxy_id=proxy.id)
                        _observe_acquisition(pool, started, wait_seconds, "reused")
                        return Lease(pool, existing, identity, proxy)
                candidate = await tx.pick_candidate(query)
                if candidate is not None:
                    proxy, drafts = await _resolve_proxy(pool, tx, candidate)
                    try:
                        row, reclaimed = await tx.acquire_lease(
                            candidate,
                            pool=pool.name,
                            holder=pool.holder,
                            ttl_seconds=pool.timing.ttl.total_seconds(),
                            idempotency_key=idempotency_key,
                            shared=pool.shared,
                            proxy_id=proxy.id if proxy else None,
                        )
                    except LeaseUnavailable:
                        # JOIN с ih_leases читает снимок начала SELECT. Под высокой конкуренцией
                        # конкурент может успеть закоммитить аренду после снимка, но до INSERT;
                        # fencing/уникальный ключ сохраняют безопасность, а выбор нужно повторить.
                        raise _SkipCandidate(candidate.id) from None
                    if reclaimed is not None:
                        drafts.append(
                            _lease_event(
                                "lease.expired", pool, reclaimed, {"expired_at": reclaimed.expires_at}
                            )
                        )
                    drafts.append(_lease_event("lease.acquired", pool, row, {"shared": pool.shared}))
                    await space._emit(tx, *drafts)
                    if idempotency_key is not None:
                        await tx.complete_idempotency_key(
                            namespace(tx.space.id, LEASE_FAMILY),
                            idempotency_key,
                            "succeeded",
                            result_type=LEASE_FAMILY,
                            result_version=1,
                            result={"lease_id": str(row.lease_id), "identity_id": str(row.identity_id)},
                        )
                    _observe_acquisition(pool, started, wait_seconds, "acquired")
                    return Lease(pool, row, candidate, proxy)
        except _SkipCandidate as skip:
            query = replace(query, exclude=query.exclude | {skip.identity_id})
            continue
        except _PoolExhausted:
            if not exhausted_reported:  # одно событие на вызов, а не на каждую итерацию ожидания
                exhausted_reported = True
                await _pool_exhausted(pool)
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            elapsed = max(0.0, clock.monotonic() - started)
            record_metric(
                space.hub.metrics,
                "histogram",
                "identity_hub_lease_acquisition_seconds",
                elapsed,
                space=space.slug,
                service=pool.service.name,
                pool=pool.name,
                status="empty",
            )
            record_metric(
                space.hub.metrics,
                "counter",
                "identity_hub_lease_empty_total",
                space=space.slug,
                service=pool.service.name,
                pool=pool.name,
            )
            if wait_seconds > 0:
                record_metric(
                    space.hub.metrics,
                    "histogram",
                    "identity_hub_lease_wait_seconds",
                    elapsed,
                    space=space.slug,
                    service=pool.service.name,
                    pool=pool.name,
                    outcome="timeout",
                )
            if explicit_query is None:  # внутренние выборки (вход, сверка) ёмкость пула не оценивают
                with contextlib.suppress(Exception):
                    await pool._check_capacity()
            raise LeaseUnavailable(
                f"пул {pool.name}: нет свободного аккаунта",
                reason=await _unavailable_reason(pool, requested, proxy_exhausted=exhausted_reported),
            )
        await clock.sleep(min(remaining, delay * (0.5 + random.random())))
        delay = min(delay * 2, 2.0)


async def _unavailable_reason(
    pool: Pool, query: CandidateQuery, *, proxy_exhausted: bool
) -> LeaseUnavailableReason | None:
    """Почему аренда не выдана. Считается только на пути отказа; сбой подсчёта причину не даёт."""
    if proxy_exhausted:
        return LeaseUnavailableReason.NO_PROXY
    try:
        async with pool.space._tx(own=True) as tx:
            busy = await tx.count_live_candidates(query, 1)
    except Exception:
        return None
    return LeaseUnavailableReason.BUSY if busy else LeaseUnavailableReason.NO_CANDIDATES


def _observe_acquisition(pool: Pool, started: float, wait_seconds: float, status: str) -> None:
    elapsed = max(0.0, pool.space.hub.clock.monotonic() - started)
    record_metric(
        pool.space.hub.metrics,
        "histogram",
        "identity_hub_lease_acquisition_seconds",
        elapsed,
        space=pool.space.slug,
        service=pool.service.name,
        pool=pool.name,
        status=status,
    )
    if wait_seconds > 0:
        record_metric(
            pool.space.hub.metrics,
            "histogram",
            "identity_hub_lease_wait_seconds",
            elapsed,
            space=pool.space.slug,
            service=pool.service.name,
            pool=pool.name,
            outcome="acquired",
        )


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
    chosen = await tx.choose_proxy(policy.group, policy.max_identities, claim=policy.sticky)
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
                    "pool": policy.group,
                },
            )
        )
    return chosen, drafts


async def _pool_exhausted(pool: Pool) -> None:
    policy = pool.proxy_policy
    assert policy is not None
    hub = pool.space.hub
    # событие внешнее и critical: каждый ждущий воркер не должен слать его в вебхуки и Telegram
    key = (pool.space.slug, pool.name, policy.group)
    now = hub.clock.monotonic()
    last = hub._proxy_exhausted_at.get(key)
    if last is not None and now - last < PROXY_EXHAUSTED_INTERVAL:
        return
    hub._proxy_exhausted_at[key] = now
    try:
        async with pool.space._tx(own=True) as tx:
            await pool.space._emit(
                tx,
                EventDraft(
                    "proxy.pool.exhausted",
                    actor=pool.holder,
                    service=pool.service.name,
                    data={"proxy_pool": policy.group, "pool": pool.name},
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
