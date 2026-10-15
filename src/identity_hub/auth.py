"""Оркестрация входа через аутентификатор сервиса (ТЗ FR-5.4–5.6, FR-4.3, §3.3).

Вход выполняется только в явных точках — `space.auth.login/login_many` и `space.auth.answer_challenge`;
внутри обычной аренды библиотека вход не делает. Связанные аккаунты (почта для рекавери и т.п.)
берутся в аренду по запросу аутентификатора в порядке «зависимый → связанный».
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pydantic import BaseModel

from identity_hub._ids import new_id
from identity_hub._space import SpaceBase, logger
from identity_hub.crypto import associated_data
from identity_hub.domain.provider_operations import RetryPolicy, safe_error_code
from identity_hub.domain.redaction import sanitize_text
from identity_hub.domain.regions import Command
from identity_hub.enums import AuthState, LeaseUnavailableReason, Lifecycle, Standing
from identity_hub.errors import (
    LeaseOwnershipLost,
    LeaseUnavailable,
    NotFound,
    ValidationFailed,
    describe_error,
)
from identity_hub.events import EventDraft
from identity_hub.operation import apply_fault_effects
from identity_hub.protocols import Authenticator, Blame, Fault, LoginChallenge, LoginFailure
from identity_hub.service import Service
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import CandidateQuery, ChallengeRow, ProviderClaim, StoreTx

if TYPE_CHECKING:
    from identity_hub.lease import Lease
    from identity_hub.pool import Pool

__all__ = ["LOGIN_POOL", "LoginMixin", "LoginOutcome", "LoginReport"]

LOGIN_POOL = "identity-hub:login"
LOGIN_AUTH_STATES = frozenset({AuthState.NO_SESSION, AuthState.SESSION_INVALID})
ALL_LIFECYCLES = frozenset({Lifecycle.PROVISIONING, Lifecycle.READY})
LOGIN_STANDING = frozenset({Standing.GOOD, Standing.LIMITED, Standing.APPEALING})
LOGIN_ABANDONED_GRACE = timedelta(seconds=60)
"""Запас сверх `Service.login_timeout`, после которого `logging_in` без аренды считается брошенным."""


class LoginOutcome(StrEnum):
    """Исход одной попытки входа; значения совпадают со строками до IMP-23."""

    SUCCEEDED = "succeeded"
    CHALLENGED = "challenged"  # платформа ждёт ответа человека — см. `answer_challenge`
    FAILED = "failed"


@dataclass
class LoginReport:
    succeeded: list[UUID] = field(default_factory=list)
    challenged: list[UUID] = field(default_factory=list)
    failed: dict[UUID, str] = field(default_factory=dict)
    # identity → операция провайдера попытки (D-222): незавершённые видны в admin.provider_operations
    operations: dict[UUID, UUID] = field(default_factory=dict)


class _LinkedIdentity:
    def __init__(self, lease: Lease) -> None:
        self._lease = lease

    @property
    def identity_id(self) -> UUID:
        return self._lease.identity_id

    @property
    def external_ref(self) -> str:
        return self._lease.identity.external_ref

    async def credentials(self) -> BaseModel | None:
        return await self._lease.credentials()

    async def credential(self, kind: str = "default", name: str = "default") -> BaseModel | None:
        return await self._lease.credential(kind, name)

    async def session(self, slot: str = DEFAULT_SLOT) -> BaseModel | None:
        return await self._lease.session(slot)


class _LoginContext:
    def __init__(
        self,
        space: LoginMixin,
        lease: Lease,
        answer: str | None,
        context: Mapping[str, Any] | None,
    ) -> None:
        self._space = space
        self._lease = lease
        self._answer = answer
        self._context = context
        self._linked: dict[str, Lease] = {}
        self.attempt_id: UUID | None = None  # claim_id операции: ключ идемпотентности для платформы

    @property
    def identity_id(self) -> UUID:
        return self._lease.identity_id

    @property
    def external_ref(self) -> str:
        return self._lease.identity.external_ref

    @property
    def proxy_url(self) -> str | None:
        return self._lease._proxy_url()

    @property
    def challenge_answer(self) -> str | None:
        return self._answer

    @property
    def challenge_context(self) -> Mapping[str, Any] | None:
        return self._context

    async def credentials(self) -> BaseModel | None:
        return await self._lease.credentials()

    async def credential(self, kind: str = "default", name: str = "default") -> BaseModel | None:
        return await self._lease.credential(kind, name)

    async def session(self, slot: str = DEFAULT_SLOT) -> BaseModel | None:
        """Последняя сохранённая сессия слота, в том числе признанная недействительной: при повторном
        входе из неё берут параметры устройства и т.п. (R2-F27). Нет сессии — None."""
        return await self._lease._stored_session(slot, valid_only=False)

    async def linked(self, role: str) -> _LinkedIdentity | None:
        """Связанный аккаунт по роли, взятый в аренду (FR-4.3). Нет связи — None."""
        if role in self._linked:
            return _LinkedIdentity(self._linked[role])
        links = await self._space._links_of(self.identity_id)  # type: ignore[attr-defined]
        link = links.linked(role)
        if link is None:
            return None
        pool: Pool = self._space._pool(LOGIN_POOL, service=link.service)  # type: ignore[attr-defined]
        query = CandidateQuery(
            service=link.service,
            standing=frozenset(Standing),
            needs_auth=False,
            among=[link.identity_id],
            lifecycles=ALL_LIFECYCLES,
            shared=pool.shared,
        )
        # занятый связанный аккаунт ждём не дольше остатка основной аренды (R2-F22)
        clock = self._space.hub.clock
        remaining = max(0.0, self._lease._deadline() - clock.monotonic())
        lease = await pool.acquire(query=query, wait=min(pool.timing.ttl.total_seconds(), remaining))
        lease._start(None)  # вход бывает долгим: связанная аренда тоже продлевается
        self._linked[role] = lease
        return _LinkedIdentity(lease)

    @property
    def lost(self) -> bool:
        """Основная или связанная аренда потеряна: heartbeat отменил задачу входа."""
        return self._lease._cancelled_by_us or any(lease._cancelled_by_us for lease in self._linked.values())

    async def close(self) -> None:
        linked, self._linked = self._linked, {}
        for lease in linked.values():
            try:  # сбой освобождения одной аренды не оставляет остальные до TTL
                await lease.release()
            except Exception as exc:
                logger.warning(
                    "не удалось освободить связанную аренду %s: %s", lease.lease_id, describe_error(exc)
                )


# вход повторяет человек или login_many: временная ошибка попытки — failed, без автоповтора
_NO_RETRY = RetryPolicy(max_attempts=1)


class LoginMixin(SpaceBase):
    async def _run_login(self, identity_id: UUID) -> LoginOutcome:
        """Run one explicit login attempt for a selected identity."""
        async with self._tx(own=True) as tx:
            identity = await self._identity(tx, identity_id)
        service = self.hub.service(identity.service)
        if service.authenticator is None:
            raise ValidationFailed(f"у сервиса {service.name} нет Authenticator")
        if identity.auth_state == AuthState.LOGGING_IN:
            # вход уже идёт в другой задаче или процессе; брошенный вход сбросит обслуживание
            raise LeaseUnavailable(
                f"аккаунт {identity_id} уже выполняет вход", reason=LeaseUnavailableReason.BUSY
            )
        if identity.auth_state not in LOGIN_AUTH_STATES:
            raise ValidationFailed(f"вход недоступен из состояния auth={identity.auth_state}")
        pool: Pool = self._pool(LOGIN_POOL, service=service)  # type: ignore[attr-defined]
        query = replace(
            pool.candidate_query(for_operation="probe"),
            standing=LOGIN_STANDING,
            lifecycles=ALL_LIFECYCLES,
            auth_states=LOGIN_AUTH_STATES,
            among=[identity_id],
        )
        lease = await pool.acquire(query=query)  # занят — сразу LeaseUnavailable (R2-F21)
        outcome, _error = await self._login(lease, service, None, None, None)
        return outcome

    async def _run_logins(self, *, service: Service | str | None = None, limit: int = 10) -> LoginReport:
        """Войти в аккаунты `no_session`/`session_invalid` сервисов с Authenticator (FR-5.4)."""
        services = [self.hub.service(service)] if service is not None else list(self.hub.services)
        report = LoginReport()
        attempts = 0
        for svc in services:
            if svc.authenticator is None:
                if service is not None:
                    raise ValidationFailed(f"у сервиса {svc.name} нет Authenticator")
                continue
            pool: Pool = self._pool(LOGIN_POOL, service=svc)  # type: ignore[attr-defined]
            query = replace(
                pool.candidate_query(for_operation="probe"),
                standing=LOGIN_STANDING,
                lifecycles=ALL_LIFECYCLES,
                auth_states=LOGIN_AUTH_STATES,
            )
            while attempts < limit:
                try:
                    lease = await pool.acquire(query=query)
                except LeaseUnavailable:
                    break
                query = replace(query, exclude=query.exclude | {lease.identity_id})  # одна попытка на аккаунт
                async with self._tx(own=True) as tx:
                    auth = await tx.load_region(lease.identity_id, "auth", DEFAULT_SLOT)
                if auth is not None and auth.retryable is False:
                    await lease.release()
                    continue
                attempts += 1
                outcome, error = await self._login(lease, svc, None, None, None, report.operations)
                _record(report, lease.identity_id, outcome, error)
        return report

    async def _answer_challenge(
        self,
        challenge_id: UUID,
        answer: str,
        *,
        actor: str = "admin",
        expected_identity_id: UUID | None = None,
    ) -> LoginOutcome:
        """Ответ человека на challenge (FR-5.5): сохраняется зашифрованным, вход продолжается сразу.

        Возвращает `LoginOutcome`: `SUCCEEDED`, `CHALLENGED` (платформа спросила ещё) или `FAILED`.
        """
        if not answer:
            raise ValidationFailed("пустой ответ на challenge")
        envelope = self.hub._envelope
        async with self._tx(own=True) as tx:
            challenge = await tx.get_challenge(challenge_id)
            if challenge is None:
                raise NotFound("challenge", challenge_id)
            if challenge.resolved_at is not None:
                raise ValidationFailed("challenge уже закрыт")
            if expected_identity_id is not None and challenge.identity_id != expected_identity_id:
                raise NotFound("challenge", challenge_id)
            now = await tx.now()
            if challenge.expires_at is not None and challenge.expires_at <= now:
                raise ValidationFailed("challenge истёк")
            identity = await self._identity(tx, challenge.identity_id)
            aad = associated_data("ih_challenges", challenge.identity_id, str(challenge.id))
            nonce, ciphertext = envelope.seal_sibling(challenge.context, aad, answer.encode(), "answer")
            if not await tx.answer_challenge(challenge_id, nonce, ciphertext):
                raise ValidationFailed("challenge уже закрыт")
            await self._emit(
                tx,
                EventDraft(
                    "identity.challenge.answered",
                    actor=actor,
                    service=identity.service,
                    identity_id=identity.id,
                    data={"challenge_id": str(challenge_id), "kind": challenge.kind},
                ),
            )
        svc = self.hub.service(identity.service)
        if svc.authenticator is None:
            raise ValidationFailed(f"у сервиса {svc.name} нет Authenticator")
        context = envelope.open_json(challenge.context, aad)
        pool: Pool = self._pool(LOGIN_POOL, service=svc)  # type: ignore[attr-defined]
        query = CandidateQuery(
            service=svc.name,
            standing=LOGIN_STANDING,
            needs_auth=False,
            among=[identity.id],
            lifecycles=ALL_LIFECYCLES,
            auth_states=frozenset({AuthState.CHALLENGE}),
            shared=pool.shared,
        )
        lease = await pool.acquire(query=query, wait=pool.timing.ttl)
        outcome, _ = await self._login(lease, svc, challenge, answer, context or None)
        return outcome

    async def _expire_challenges(self, *, limit: int = 100) -> int:
        """Закрыть просроченные challenge: auth → no_session, событие `identity.challenge.expired`."""
        async with self._tx(own=True) as tx:
            expired = await tx.expired_challenges(limit)
        count = 0
        known = {service.name for service in self.hub.services}
        for challenge in expired:
            async with self._tx(own=True) as tx:
                if not await tx.resolve_challenge(challenge.id):
                    continue
                identity = await self._identity(tx, challenge.identity_id)
                # сервис убран из кода: вопрос закрывается, состояние аккаунта не трогаем (R2-F06)
                if identity.auth_state == AuthState.CHALLENGE and identity.service in known:
                    await apply_region_change(
                        self,
                        tx,
                        identity.id,
                        "auth",
                        DEFAULT_SLOT,
                        Command(AuthState.NO_SESSION, actor="timer"),
                    )
                await self._emit(
                    tx,
                    EventDraft(
                        "identity.challenge.expired",
                        actor="timer",
                        service=identity.service,
                        identity_id=identity.id,
                        data={"challenge_id": str(challenge.id), "kind": challenge.kind},
                    ),
                )
                count += 1
        return count

    async def _expire_logins(self, *, limit: int = 100) -> int:
        """`logging_in` без живой аренды дольше срока входа → `no_session` (процесс упал во время входа)."""
        async with self._tx(own=True) as tx:
            now = await tx.now()
            stuck = await tx.stuck_logins(limit)
        count = 0
        for identity_id, since in stuck:
            async with self._tx(own=True) as tx:
                identity = await tx.get_identity(identity_id)
                if identity is None or identity.auth_state != AuthState.LOGGING_IN:
                    continue
                if all(identity.service != known.name for known in self.hub.services):
                    continue  # сервис убран из кода: аккаунт не останавливает обслуживание (R2-F06)
                timeout = self.hub.service(identity.service).login_timeout + LOGIN_ABANDONED_GRACE
                if since + timeout > now or await tx.current_lease(identity_id) is not None:
                    continue
                await apply_region_change(
                    self, tx, identity_id, "auth", DEFAULT_SLOT, Command(AuthState.NO_SESSION, actor="timer")
                )
                await self._emit(
                    tx,
                    EventDraft(
                        "identity.login.failed",
                        actor="timer",
                        service=identity.service,
                        identity_id=identity_id,
                        data={"error": "LoginAbandoned", "code": "LoginAbandoned"},
                    ),
                )
                count += 1
        return count

    # --------------------------------------------------------- внутреннее

    async def _login(
        self,
        lease: Lease,
        svc: Service,
        challenge: ChallengeRow | None,
        answer: str | None,
        context: Mapping[str, Any] | None,
        operations: dict[UUID, UUID] | None = None,
    ) -> tuple[LoginOutcome, str | None]:
        """Один вход под арендой `lease`; аренда и связанные аренды освобождаются здесь.

        После перевода в `logging_in` любое завершение, кроме успеха и нового challenge, возвращает
        `auth` в состояние до входа — в том числе при отмене задачи (под `shield`). Сбой самого
        аутентификатора — `no_session`, как и раньше.
        """
        assert svc.authenticator is not None
        ctx = _LoginContext(self, lease, answer, context)
        initial = AuthState(lease.identity.auth_state)
        entered = False
        lease._start(None)  # heartbeat: вход с SMS-кодом легко длится дольше ttl − safety_margin
        try:
            # D-222: операция login — claim коммитится до вызова адаптера, итог — одной транзакцией
            claim = await self._claim_login(lease, svc)
            ctx.attempt_id = claim.claim_id
            if operations is not None:
                operations[lease.identity_id] = claim.operation_id
            await self._set_auth(lease, AuthState.LOGGING_IN)
            entered = True
            try:
                async with asyncio.timeout(svc.login_timeout.total_seconds()):
                    # функция входа приведена к объекту протокола в Service.__post_init__
                    session = await cast(Authenticator, svc.authenticator).login(ctx)
            except LoginChallenge as asked:
                await self._open_challenge(lease, asked, claim=claim, previous=challenge)
                entered = False
                return LoginOutcome.CHALLENGED, None
            except LoginFailure as failure:
                fault = failure.fault
                if fault is not None and failure.raw_status is not None and fault.raw_status is None:
                    fault = replace(fault, raw_status=failure.raw_status)
                if fault is not None and fault.blame is Blame.SESSION_INVALID:
                    # auth-код отказа принадлежит LoginFailure; Observation сохраняет raw_status
                    # и сразу делает единственный переход logging_in → session_invalid.
                    fault = replace(fault, code=failure.code)
                target = (
                    AuthState.SESSION_INVALID
                    if fault is not None and fault.blame is Blame.SESSION_INVALID
                    else AuthState.NO_SESSION
                )
                await self._set_auth(
                    lease,
                    target,
                    "identity.login.failed",
                    {
                        "error": "LoginFailure",
                        "code": failure.code,
                        "retryable": failure.retryable,
                    },
                    claim=claim,
                    outcome="failed",
                    result={"outcome": "failed", "retryable": failure.retryable},
                    error_code=failure.code,
                    auth_code=failure.code,
                    auth_retryable=failure.retryable,
                    fault=fault,
                    previous=challenge,
                )
                if fault is not None and fault.blame is Blame.PROXY and lease.proxy_id is not None:
                    try:
                        await self._report_proxy_failure(  # type: ignore[attr-defined]
                            lease.proxy_id,
                            weight=fault.weight,
                            actor=lease.holder,
                            raw_status=fault.raw_status,
                        )
                    except Exception:
                        logger.exception("не удалось учесть ошибку прокси %s", lease.proxy_id)
                entered = False
                return LoginOutcome.FAILED, f"LoginFailure: {failure.code}"
            except Exception as exc:
                name = "LoginTimeout" if isinstance(exc, TimeoutError) else type(exc).__name__
                code = safe_error_code(exc) if name != "LoginTimeout" else name
                await self._set_auth(
                    lease,
                    AuthState.NO_SESSION,
                    "identity.login.failed",
                    {"error": name, "code": code},
                    claim=claim,
                    outcome="failed",
                    error_code=code,
                    previous=challenge,
                )
                entered = False
                return LoginOutcome.FAILED, name
            # новый вход — новая запись сессии, прежняя остаётся в истории отозванной (D-219)
            payload, session_expires_at = lease._seal_session_with_expiry(session, DEFAULT_SLOT)
            await self._set_auth(
                lease,
                AuthState.AUTHENTICATED,
                "identity.login.succeeded",
                {},
                claim=claim,
                outcome="succeeded",
                result={"outcome": "succeeded"},
                session=payload,
                session_expires_at=session_expires_at,
                previous=challenge,
            )
            entered = False
            return LoginOutcome.SUCCEEDED, None
        except asyncio.CancelledError:
            if entered:
                await asyncio.shield(self._restore_auth(lease, initial, challenge, "CancelledError"))
            if not ctx.lost:
                raise  # отмена вызывающего: состояние возвращено, отмена продолжается
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()  # задачу отменил heartbeat потерянной аренды — это исход входа
            return LoginOutcome.FAILED, "LeaseLost"
        except Exception as exc:
            logger.warning("вход аккаунта %s прерван: %s", lease.identity_id, describe_error(exc))
            if entered:
                await asyncio.shield(self._restore_auth(lease, initial, challenge, type(exc).__name__))
            return LoginOutcome.FAILED, type(exc).__name__
        finally:
            await ctx.close()
            await lease.release()

    async def _restore_auth(
        self, lease: Lease, initial: AuthState, challenge: ChallengeRow | None, error: str
    ) -> None:
        """Вернуть `auth` в состояние до входа. Не бросает: если аренда уже потеряна, сбросит обслуживание."""
        try:
            async with self._tx(own=True) as tx:
                await lease._check_fencing(tx)
                target = initial
                if initial is AuthState.CHALLENGE:
                    # ответ на challenge уже использован: без открытого вопроса возвращаться некуда
                    current = await tx.get_challenge(challenge.id) if challenge is not None else None
                    if current is None or current.resolved_at is not None:
                        target = AuthState.NO_SESSION
                await apply_region_change(
                    self,
                    tx,
                    lease.identity_id,
                    "auth",
                    DEFAULT_SLOT,
                    Command(target, actor=lease.holder),
                    lease_id=lease.lease_id,
                )
                await self._emit(
                    tx,
                    EventDraft(
                        "identity.login.failed",
                        actor=lease.holder,
                        service=lease.service,
                        identity_id=lease.identity_id,
                        lease_id=lease.lease_id,
                        data={"error": error, "code": error},
                    ),
                )
        except Exception as exc:
            logger.warning(
                "не удалось вернуть auth аккаунта %s после прерванного входа: %s",
                lease.identity_id,
                describe_error(exc),
            )

    async def _claim_login(self, lease: Lease, svc: Service) -> ProviderClaim:
        """Поставить и взять операцию login одной короткой транзакцией — до вызова адаптера (D-222)."""
        claim_for = svc.login_timeout + timedelta(minutes=1)
        async with self._tx(own=True) as tx:
            await lease._check_fencing(tx)
            operation = await tx.enqueue_provider_operation(lease.identity_id, "login")
            (claim,) = await tx.claim_provider_operations(
                lease.holder, limit=1, claim_for=claim_for, operation_ids=[operation.id]
            )
        return claim

    @staticmethod
    async def _apply_login(
        tx: StoreTx,
        lease: Lease,
        claim: ProviderClaim | None,
        outcome: str,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        """Итог операции login в транзакции итога; поздний ответ — `LeaseOwnershipLost`, всё откатится."""
        if claim is None:
            return
        applied = await tx.apply_provider_operation(
            claim, outcome, result=result, error_code=error_code, fence=lease.fence, policy=_NO_RETRY
        )
        if not applied:
            raise LeaseOwnershipLost(
                lease.lease_id, "late", identity_id=lease.identity_id, fencing=lease.fencing
            )

    async def _set_auth(
        self,
        lease: Lease,
        state: AuthState,
        event: str | None = None,
        data: Mapping[str, Any] | None = None,
        *,
        claim: ProviderClaim | None = None,
        outcome: str = "succeeded",
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        auth_code: str | None = None,
        auth_retryable: bool | None = None,
        fault: Fault | None = None,
        session: Any = None,
        session_expires_at: datetime | None = None,
        previous: ChallengeRow | None = None,
    ) -> None:
        async with self._tx(own=True) as tx:
            await lease._check_fencing(tx)
            await self._apply_login(tx, lease, claim, outcome, result=result, error_code=error_code)
            if fault is not None:
                await apply_fault_effects(lease, tx, fault)
            if session is not None:
                await lease._write_session(
                    tx,
                    session,
                    DEFAULT_SLOT,
                    mode="replace",
                    expires_at=session_expires_at,
                )
            if previous is not None:
                await tx.resolve_challenge(previous.id)
            await apply_region_change(
                self,
                tx,
                lease.identity_id,
                "auth",
                DEFAULT_SLOT,
                Command(state, actor=lease.holder, code=auth_code, retryable=auth_retryable),
                lease_id=lease.lease_id,
            )
            if event is not None:
                await self._emit(
                    tx,
                    EventDraft(
                        event,
                        actor=lease.holder,
                        service=lease.service,
                        identity_id=lease.identity_id,
                        lease_id=lease.lease_id,
                        data=dict(data or {}),
                    ),
                )

    async def _open_challenge(
        self,
        lease: Lease,
        asked: LoginChallenge,
        *,
        claim: ProviderClaim | None = None,
        previous: ChallengeRow | None = None,
    ) -> None:
        challenge_id = new_id()
        aad = associated_data("ih_challenges", lease.identity_id, str(challenge_id))
        context = asked.context if asked.context is not None else {}
        payload = self.hub._envelope.seal_model(context, aad)
        hint = sanitize_text(asked.hint) or ""  # уходит во внешнее событие и очередь человека
        async with self._tx(own=True) as tx:
            await lease._check_fencing(tx)
            # попытка входа завершилась вопросом платформы: операция succeeded, исход — challenged
            await self._apply_login(tx, lease, claim, "succeeded", result={"outcome": "challenged"})
            if previous is not None:
                await tx.resolve_challenge(previous.id)
            now = await tx.now()
            expires_at = now + asked.expires_in if asked.expires_in else now + timedelta(hours=1)
            opened = await tx.open_challenge(
                challenge_id, lease.identity_id, asked.kind, hint, payload, expires_at
            )
            if not opened:
                # у аккаунта уже открыт вопрос (FR-5.5): вход считается неудачным, состояние не трогаем
                raise ValidationFailed(f"у аккаунта {lease.identity_id} уже открыт challenge")
            await apply_region_change(
                self,
                tx,
                lease.identity_id,
                "auth",
                DEFAULT_SLOT,
                Command(AuthState.CHALLENGE, actor=lease.holder),
                lease_id=lease.lease_id,
            )
            await self._emit(
                tx,
                EventDraft(
                    "identity.challenge.opened",
                    actor=lease.holder,
                    service=lease.service,
                    identity_id=lease.identity_id,
                    lease_id=lease.lease_id,
                    data={
                        "challenge_id": str(challenge_id),
                        "kind": asked.kind,
                        "hint": hint,
                        "expires_at": expires_at,
                    },
                ),
            )

    async def _resolve(self, challenge_id: UUID) -> None:
        async with self._tx(own=True) as tx:
            await tx.resolve_challenge(challenge_id)


def _record(report: LoginReport, identity_id: UUID, outcome: LoginOutcome, error: str | None) -> None:
    if outcome == "succeeded":
        report.succeeded.append(identity_id)
    elif outcome == "challenged":
        report.challenged.append(identity_id)
    else:
        report.failed[identity_id] = error or "unknown"
