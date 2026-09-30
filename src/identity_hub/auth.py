"""Оркестрация входа через аутентификатор сервиса (ТЗ FR-5.4–5.6, FR-4.3, §3.3).

Вход выполняется только в явных точках — `space.auth.login/login_many` и `space.auth.answer_challenge`;
внутри обычной аренды библиотека вход не делает. Связанные аккаунты (почта для рекавери и т.п.)
берутся в аренду по запросу аутентификатора в порядке «зависимый → связанный».
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel

from identity_hub._ids import new_id
from identity_hub._space import SpaceBase, logger
from identity_hub.api._base import legacy
from identity_hub.crypto import associated_data
from identity_hub.domain.regions import Command
from identity_hub.enums import AuthState, Lifecycle, Standing
from identity_hub.errors import LeaseUnavailable, NotFound, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.protocols import LoginChallenge
from identity_hub.service import Service
from identity_hub.state import DEFAULT_SLOT, apply_region_change
from identity_hub.store.base import CandidateQuery, ChallengeRow

if TYPE_CHECKING:
    from identity_hub.lease import Lease
    from identity_hub.pool import Pool

__all__ = ["LOGIN_POOL", "LoginMixin", "LoginOutcome", "LoginReport"]

LOGIN_POOL = "identity-hub:login"
LOGIN_AUTH_STATES = frozenset({AuthState.NO_SESSION, AuthState.SESSION_INVALID})
ALL_LIFECYCLES = frozenset({Lifecycle.PROVISIONING, Lifecycle.READY})
LOGIN_STANDING = frozenset({Standing.GOOD, Standing.LIMITED, Standing.APPEALING})


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

    @property
    def identity_id(self) -> UUID:
        return self._lease.identity_id

    @property
    def external_ref(self) -> str:
        return self._lease.identity.external_ref

    @property
    def proxy_url(self) -> str | None:
        return self._lease.proxy

    @property
    def challenge_answer(self) -> str | None:
        return self._answer

    @property
    def challenge_context(self) -> Mapping[str, Any] | None:
        return self._context

    async def credentials(self) -> BaseModel | None:
        return await self._lease.credentials()

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
        lease = await pool.acquire(query=query, wait=pool.timing.ttl)
        self._linked[role] = lease
        return _LinkedIdentity(lease)

    async def close(self) -> None:
        for lease in self._linked.values():
            await lease.release()
        self._linked.clear()


class LoginMixin(SpaceBase):
    async def _run_login(self, identity_id: UUID) -> LoginOutcome:
        """Run one explicit login attempt for a selected identity."""
        async with self._tx(own=True) as tx:
            identity = await self._identity(tx, identity_id)
        service = self.hub.service(identity.service)
        if service.authenticator is None:
            raise ValidationFailed(f"у сервиса {service.name} нет Authenticator")
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
        lease = await pool.acquire(query=query, wait=pool.timing.ttl)
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
                attempts += 1
                query = replace(query, exclude=query.exclude | {lease.identity_id})  # одна попытка на аккаунт
                outcome, error = await self._login(lease, svc, None, None, None)
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
        envelope = self.hub.envelope
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
        for challenge in expired:
            async with self._tx(own=True) as tx:
                if not await tx.resolve_challenge(challenge.id):
                    continue
                identity = await self._identity(tx, challenge.identity_id)
                if identity.auth_state == AuthState.CHALLENGE:
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

    # --------------------------------------------------------- внутреннее

    async def _login(
        self,
        lease: Lease,
        svc: Service,
        challenge: ChallengeRow | None,
        answer: str | None,
        context: Mapping[str, Any] | None,
    ) -> tuple[LoginOutcome, str | None]:
        assert svc.authenticator is not None
        ctx = _LoginContext(self, lease, answer, context)
        try:
            await self._set_auth(lease, AuthState.LOGGING_IN)
            try:
                session = await svc.authenticator.login(ctx)
            except LoginChallenge as asked:
                if challenge is not None:
                    await self._resolve(challenge.id)
                await self._open_challenge(lease, asked)
                return LoginOutcome.CHALLENGED, None
            except Exception as exc:
                if challenge is not None:
                    await self._resolve(challenge.id)
                error = f"{type(exc).__name__}: {exc}"[:500]
                await self._set_auth(
                    lease, AuthState.NO_SESSION, "identity.login.failed", {"error": type(exc).__name__}
                )
                return LoginOutcome.FAILED, error
            await lease.save_session(session)
            if challenge is not None:
                await self._resolve(challenge.id)
            await self._set_auth(lease, AuthState.AUTHENTICATED, "identity.login.succeeded", {})
            return LoginOutcome.SUCCEEDED, None
        except Exception as exc:
            logger.exception("вход аккаунта %s прерван", lease.identity_id)
            return LoginOutcome.FAILED, f"{type(exc).__name__}: {exc}"[:500]
        finally:
            await ctx.close()
            await lease.release()

    async def _set_auth(
        self, lease: Lease, state: AuthState, event: str | None = None, data: Mapping[str, Any] | None = None
    ) -> None:
        async with self._tx(own=True) as tx:
            await lease._check_fencing(tx)
            await apply_region_change(
                self,
                tx,
                lease.identity_id,
                "auth",
                DEFAULT_SLOT,
                Command(state, actor=lease.holder),
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

    async def _open_challenge(self, lease: Lease, asked: LoginChallenge) -> None:
        challenge_id = new_id()
        aad = associated_data("ih_challenges", lease.identity_id, str(challenge_id))
        context = asked.context if asked.context is not None else {}
        payload = self.hub.envelope.seal_model(context, aad)
        async with self._tx(own=True) as tx:
            await lease._check_fencing(tx)
            now = await tx.now()
            expires_at = now + asked.expires_in if asked.expires_in else now + timedelta(hours=1)
            opened = await tx.open_challenge(
                challenge_id, lease.identity_id, asked.kind, asked.hint, payload, expires_at
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
                        "hint": asked.hint,
                        "expires_at": expires_at,
                    },
                ),
            )

    async def _resolve(self, challenge_id: UUID) -> None:
        async with self._tx(own=True) as tx:
            await tx.resolve_challenge(challenge_id)

    run_login = legacy(_run_login, "run_login")
    run_logins = legacy(_run_logins, "run_logins")
    answer_challenge = legacy(_answer_challenge, "answer_challenge")

    expire_challenges = legacy(_expire_challenges, "expire_challenges")


def _record(report: LoginReport, identity_id: UUID, outcome: LoginOutcome, error: str | None) -> None:
    if outcome == "succeeded":
        report.succeeded.append(identity_id)
    elif outcome == "challenged":
        report.challenged.append(identity_id)
    else:
        report.failed[identity_id] = error or "unknown"
