"""`auth.revalidate()`: явная перепроверка аккаунта из любого состояния (D-335, L3).

Диагностика не меняет `lifecycle`, `availability` и `standing` командой и не переводит `auth` в
`logging_in`: вердикт провайдера применяется как наблюдение, а признаком «идёт» служит claim
провайдерной операции. Что библиотека применила и чего нет (`sticky`, `pinned`), вызывающий видит в
`AuthRevalidationResult.applied`; итоговые статусы приложения (`alive`, `dead`) считает само приложение.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from identity_hub._space import SpaceBase, logger
from identity_hub.domain.regions import Observation
from identity_hub.enums import AuthState, ChangeSource, LeaseUnavailableReason
from identity_hub.errors import (
    Blocker,
    ConcurrentModification,
    LeaseOwnershipLost,
    LeaseUnavailable,
    OperationNotAllowed,
    ValidationFailed,
    describe_error,
)
from identity_hub.events import EventDraft
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
    FAILED = "failed"
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
        self, identity_id: UUID, *, actor: str, reason: str, request_id: str
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
            return await self._revalidate_session(lease, service, actor)
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


def _in_progress(identity_id: UUID) -> AuthRevalidationResult:
    return AuthRevalidationResult(
        identity_id, RevalidationStatus.IN_PROGRESS, blockers=(Blocker("busy", "revalidation"),)
    )
