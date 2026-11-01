"""`auth.revalidate()`: явная перепроверка аккаунта из любого состояния (D-335, L3).

Диагностика не меняет `lifecycle`, `availability` и `standing` командой и не переводит `auth` в
`logging_in`: вердикт провайдера применяется как наблюдение, а признаком «идёт» служит claim
провайдерной операции. Что библиотека применила и чего нет (`sticky`, `pinned`), вызывающий видит в
`AuthRevalidationResult.applied`; итоговые статусы приложения (`alive`, `dead`) считает само приложение.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase, logger
from identity_hub.auth import _CredentialMark, _LoginContext
from identity_hub.domain.credentials import DISABLED, REVOKED, is_expired
from identity_hub.domain.provider_operations import safe_error_code
from identity_hub.domain.regions import Observation
from identity_hub.enums import AuthState, ChangeSource, LeaseUnavailableReason
from identity_hub.errors import (
    AlreadyExists,
    Blocker,
    ConcurrentModification,
    LeaseOwnershipLost,
    LeaseUnavailable,
    OperationNotAllowed,
    ValidationFailed,
    describe_error,
)
from identity_hub.events import EventDraft
from identity_hub.protocols import Blame, LoginChallenge, LoginFailure
from identity_hub.state import DEFAULT_SLOT, apply_region_change

__all__ = [
    "AuthRevalidationResult",
    "CredentialVerdict",
    "RevalidationMixin",
    "RevalidationStatus",
    "SessionVerdict",
]

_REQUEST_ID_LIMIT = 128
_INTERACTIVE = frozenset({AuthState.CHALLENGE, AuthState.LOGGING_IN})


class RevalidationStatus(StrEnum):
    COMPLETED = "completed"  # проверка выполнена; вердикты — в полях результата
    BLOCKED = "blocked"  # допуск или credential не позволяют; провайдер не вызывался
    IN_PROGRESS = "in_progress"  # аккаунт или операция заняты другой проверкой
    FAILED = "failed"  # технический сбой; состояние аккаунта не менялось, повтор безопасен


class SessionVerdict(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    MISSING = "missing"  # у аккаунта нет пригодной сессии default; провайдер не вызывался
    CHALLENGE = "challenge"  # провайдер требует ввода (challenge)
    FAILED = "failed"
    NOT_CHECKED = "not_checked"


class CredentialVerdict(StrEnum):
    VALID = "valid"
    REJECTED = "rejected"
    CHALLENGE = "challenge"
    BLOCKED = "blocked"
    FAILED = "failed"  # технический сбой, состояние не менялось
    # провайдер отказал не из-за credential (например, аккаунт забанен); причина в code и applied
    INCONCLUSIVE = "inconclusive"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True, slots=True)
class AuthRevalidationResult:
    """Структурный итог перепроверки; секретов не содержит."""

    identity_id: UUID
    status: RevalidationStatus
    session_verdict: SessionVerdict = SessionVerdict.NOT_CHECKED
    credential_verdict: CredentialVerdict = CredentialVerdict.NOT_CHECKED
    # регион → исход машины состояний (`applied`, `unchanged`, `sticky`, `pinned`, …)
    applied: dict[str, str] = field(default_factory=dict)
    blockers: tuple[Blocker, ...] = ()
    operation_ids: tuple[UUID, ...] = ()
    retryable: bool = False
    challenge_id: UUID | None = None
    code: str | None = None


def _check_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > _REQUEST_ID_LIMIT:
        raise ValidationFailed(f"request_id: непустая строка до {_REQUEST_ID_LIMIT} символов")


class RevalidationMixin(SpaceBase):
    async def _revalidate(
        self,
        identity_id: UUID,
        *,
        actor: str,
        reason: str,
        request_id: str,
        credential: tuple[str, str] | None = None,
    ) -> AuthRevalidationResult:
        _check_request_id(request_id)
        async with self._tx(own=True) as tx:
            row = await tx.get_identity(identity_id)
        if row is None:
            return AuthRevalidationResult(
                identity_id, RevalidationStatus.BLOCKED, blockers=(Blocker("not_found", str(identity_id)),)
            )
        service = self.hub.service(row.service)
        if service.probes is None or getattr(service.probes, "auth", None) is None:
            raise ValidationFailed(f"у сервиса {service.name} нет auth probe")
        key: tuple[str, str] | None = None
        if credential is not None:
            key = service.credential_key(*credential)
            if service.authenticator is None:
                raise ValidationFailed(f"у сервиса {service.name} нет Authenticator")
        sync: Any = self
        try:
            lease = await sync._diagnostic_lease(identity_id, actor=actor, reason=reason)
        except OperationNotAllowed as exc:
            return AuthRevalidationResult(
                identity_id, RevalidationStatus.BLOCKED, blockers=tuple(exc.blockers)
            )
        except LeaseUnavailable as exc:
            if exc.reason is not LeaseUnavailableReason.BUSY:
                raise
            return _in_progress(identity_id)
        try:
            result = await self._revalidate_session(lease, service, actor)
            if (
                key is not None
                and result.status is RevalidationStatus.COMPLETED
                and result.session_verdict in {SessionVerdict.INVALID, SessionVerdict.MISSING}
            ):
                result = await self._revalidate_credential(lease, service, actor, key, result)
            return result
        finally:
            await lease.release()

    async def _revalidate_session(self, lease: Any, service: Any, actor: str) -> AuthRevalidationResult:
        """Шаг сессии: один вызов auth probe default-слота и проекция вердикта наблюдением."""
        sync: Any = self
        identity_id: UUID = lease.identity_id
        async with self._tx(own=True) as tx:
            _credentials, sessions = await tx.access_metadata(identity_id)
            now = await tx.now()
        metadata = next((item for item in sessions if item.slot == DEFAULT_SLOT), None)
        if (
            metadata is None
            or metadata.status != "active"
            or (metadata.expires_at is not None and metadata.expires_at <= now)
        ):
            return AuthRevalidationResult(
                identity_id, RevalidationStatus.COMPLETED, session_verdict=SessionVerdict.MISSING
            )
        source_revision = await lease.session_revision(DEFAULT_SLOT)
        try:
            call = await self._targeted_session_probe(lease, service, DEFAULT_SLOT)  # type: ignore[attr-defined]
        except LeaseUnavailable as exc:
            if exc.reason is not LeaseUnavailableReason.BUSY:
                raise
            return _in_progress(identity_id)
        claim = call.claim
        if call.error_code is not None:
            return AuthRevalidationResult(
                identity_id,
                RevalidationStatus.FAILED,
                session_verdict=SessionVerdict.FAILED,
                operation_ids=(claim.operation_id,),
                retryable=True,
                code=call.error_code,
            )
        result = call.result
        try:
            state = None if result is None or result.state is None else AuthState(result.state)
        except ValueError:
            state = None
        verdicts: dict[AuthState | None, SessionVerdict] = {
            AuthState.AUTHENTICATED: SessionVerdict.VALID,
            AuthState.NO_SESSION: SessionVerdict.INVALID,
            AuthState.SESSION_INVALID: SessionVerdict.INVALID,
            AuthState.CHALLENGE: SessionVerdict.CHALLENGE,
        }
        verdict = verdicts.get(state, SessionVerdict.FAILED)
        applied: dict[str, str] = {}
        async with (
            sync._projection_guard(lease, claim, str(verdict)),
            self._tx(own=True) as tx,
        ):
            await lease._check_fencing(tx)
            actual_revision = await lease._session_revision(tx, DEFAULT_SLOT)
            if actual_revision != source_revision:
                raise ConcurrentModification(
                    "session", f"{identity_id}/{DEFAULT_SLOT}", source_revision, actual_revision
                )
            if verdict in {SessionVerdict.VALID, SessionVerdict.INVALID}:
                rejected = verdict is SessionVerdict.INVALID
                await tx.mark_session_validity(
                    identity_id, DEFAULT_SLOT, rejected=rejected, code="session_invalid" if rejected else None
                )
                await self._emit(
                    tx,
                    EventDraft(
                        "identity.session.updated",
                        actor=actor,
                        service=service.name,
                        identity_id=identity_id,
                        lease_id=lease.lease_id,
                        data={"slot": DEFAULT_SLOT, "mode": "reject" if rejected else "verify"},
                    ),
                )
                current = await tx.get_identity(identity_id)
                if current is not None and AuthState(current.auth_state) in _INTERACTIVE:
                    # интерактивный вход не затираем и не закрываем (D-335): исход тот же, что у sticky
                    applied["auth"] = "sticky"
                else:
                    decision = await apply_region_change(
                        self,
                        tx,
                        identity_id,
                        "auth",
                        DEFAULT_SLOT,
                        Observation(
                            AuthState.SESSION_INVALID if rejected else AuthState.AUTHENTICATED,
                            observed_at=await tx.now(),
                            source=ChangeSource.PROBE,
                            code="session_invalid" if rejected else None,
                            actor=actor,
                            retryable=False if rejected else None,
                        ),
                        lease_id=lease.lease_id,
                    )
                    applied["auth"] = str(decision.outcome)
            await sync._apply_operation(tx, lease, claim, "succeeded", result={"outcome": str(verdict)})
        if verdict is SessionVerdict.FAILED:
            return AuthRevalidationResult(
                identity_id,
                RevalidationStatus.FAILED,
                session_verdict=verdict,
                operation_ids=(claim.operation_id,),
                retryable=True,
            )
        if service.probes is not None and getattr(service.probes, "standing", None) is not None:
            try:
                outcomes = await sync._probe(lease, service, [("standing", "")])
            except LeaseOwnershipLost:
                raise
            except Exception as exc:
                logger.warning("проба standing при перепроверке %s: %s", identity_id, describe_error(exc))
                applied["standing"] = "failed"
            else:
                if outcomes.get("standing") not in {None, "not_probed"}:
                    applied["standing"] = outcomes["standing"]
        return AuthRevalidationResult(
            identity_id,
            RevalidationStatus.COMPLETED,
            session_verdict=verdict,
            applied=applied,
            operation_ids=(claim.operation_id,),
            challenge_id=None,
        )

    async def _revalidate_credential(
        self,
        lease: Any,
        service: Any,
        actor: str,
        key: tuple[str, str],
        base: AuthRevalidationResult,
    ) -> AuthRevalidationResult:
        """Шаг credential: вызов аутентификатора без `logging_in`, итог применяется наблюдением (D-335)."""
        sync: Any = self
        identity_id: UUID = lease.identity_id
        kind, name = key
        async with self._tx(own=True) as tx:
            open_challenge = await tx.open_challenge_of(identity_id)
            found = await tx.get_credential(identity_id, kind, name)
            now = await tx.now()
        if open_challenge is not None:  # активный вопрос не перезапускаем и провайдера не вызываем
            return replace(
                base, credential_verdict=CredentialVerdict.CHALLENGE, challenge_id=open_challenge.id
            )
        blocker: str | None = None
        if found is None:
            blocker = "credential_missing"
        elif found[1].lifecycle == REVOKED:
            blocker = "credential_revoked"
        elif found[1].lifecycle == DISABLED:
            blocker = "credential_disabled"
        elif is_expired(found[1].lifecycle, found[1].expires_at, now):
            blocker = "credential_expired"
        if blocker is not None:
            return replace(
                base,
                status=RevalidationStatus.BLOCKED,
                credential_verdict=CredentialVerdict.BLOCKED,
                blockers=(Blocker(blocker, f"{kind}/{name}"),),
            )
        ctx = _LoginContext(self, lease, None, None, requested_credential=key)  # type: ignore[arg-type]
        lease._start(None)  # вход бывает долгим: аренда продлевается
        try:
            try:
                claim = await sync._claim_login(lease, service)
            except AlreadyExists:
                return replace(
                    base,
                    status=RevalidationStatus.IN_PROGRESS,
                    blockers=(Blocker("busy", "login"),),
                )
            ctx.attempt_id = claim.claim_id
            operation_ids = (*base.operation_ids, claim.operation_id)
            applied = dict(base.applied)
            try:
                async with asyncio.timeout(service.login_timeout.total_seconds()):
                    session = await service.authenticator.login(ctx)
            except LoginChallenge as asked:
                challenge_id, outcome = await sync._open_challenge(
                    lease, asked, claim=claim, credential_marks=ctx._credential_marks, observe=True
                )
                applied["auth"] = outcome
                return replace(
                    base,
                    credential_verdict=CredentialVerdict.CHALLENGE,
                    challenge_id=challenge_id,
                    applied=applied,
                    operation_ids=operation_ids,
                )
            except LoginFailure as failure:
                if failure.credential is not None:
                    fkind, fname = service.credential_key(*failure.credential)
                    ctx._credential_marks.append(_CredentialMark(fkind, fname, True, failure.code))
                fault = failure.fault
                if fault is not None and failure.raw_status is not None and fault.raw_status is None:
                    fault = replace(fault, raw_status=failure.raw_status)
                if fault is not None and fault.blame is Blame.SESSION_INVALID:
                    fault = replace(fault, code=failure.code)
                target = (
                    AuthState.SESSION_INVALID
                    if fault is not None and fault.blame is Blame.SESSION_INVALID
                    else AuthState.NO_SESSION
                )
                applied["auth"] = await sync._set_auth(
                    lease,
                    target,
                    "identity.login.failed",
                    {"error": "LoginFailure", "code": failure.code, "retryable": failure.retryable},
                    claim=claim,
                    outcome="failed",
                    result={"outcome": "failed", "retryable": failure.retryable},
                    error_code=failure.code,
                    auth_code=failure.code,
                    auth_retryable=failure.retryable,
                    fault=fault,
                    credential_marks=ctx._credential_marks,
                    observe=True,
                )
                return replace(
                    base,
                    credential_verdict=(
                        CredentialVerdict.REJECTED
                        if failure.credential is not None
                        else CredentialVerdict.INCONCLUSIVE
                    ),
                    applied=applied,
                    operation_ids=operation_ids,
                    retryable=failure.retryable,
                    code=failure.code,
                )
            except Exception as exc:
                # технический сбой: ответа провайдера нет, ни auth, ни credential не меняем (D-335)
                code = "LoginTimeout" if isinstance(exc, TimeoutError) else safe_error_code(exc)
                await sync._finish_probe(lease, claim, "failed", error_code=code)
                return replace(
                    base,
                    status=RevalidationStatus.FAILED,
                    credential_verdict=CredentialVerdict.FAILED,
                    operation_ids=operation_ids,
                    retryable=True,
                    code=code,
                )
            payload, session_expires_at, session_fingerprint = lease._seal_session_with_expiry(
                session, DEFAULT_SLOT
            )
            marks = ctx._credential_marks
            if not any(mark.kind == kind and mark.name == name for mark in marks):
                marks.append(_CredentialMark(kind, name, False))  # аутентификатор принял именно эту запись
            applied["auth"] = await sync._set_auth(
                lease,
                AuthState.AUTHENTICATED,
                "identity.login.succeeded",
                {},
                claim=claim,
                outcome="succeeded",
                result={"outcome": "succeeded"},
                session=payload,
                session_expires_at=session_expires_at,
                session_fingerprint=session_fingerprint,
                credential_marks=marks,
                observe=True,
            )
            return replace(
                base,
                credential_verdict=CredentialVerdict.VALID,
                applied=applied,
                operation_ids=operation_ids,
            )
        finally:
            await ctx.close()


def _in_progress(identity_id: UUID) -> AuthRevalidationResult:
    return AuthRevalidationResult(
        identity_id, RevalidationStatus.IN_PROGRESS, blockers=(Blocker("busy", "revalidation"),)
    )
