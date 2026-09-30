"""One safe execution boundary for all administration commands."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeVar
from uuid import UUID

from identity_hub.errors import (
    AlreadyExists,
    ChallengeAlreadyOpen,
    ConcurrentModification,
    IdentityHubError,
    LeaseUnavailable,
    NotFound,
    OperationNotAllowed,
    QuotaExhausted,
    TransitionNotAllowed,
    ValidationFailed,
    VerificationRequirementsNotMet,
)
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.store import PgStore
from litestar import Request
from litestar.datastructures import State
from litestar.exceptions import HTTPException

from identity_hub_admin.api.contracts import MutationContext
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.context import AdminCommandContext, resolve_space_context
from identity_hub_admin.idempotency import IdempotencyCoordinator, request_fingerprint

ResultT = TypeVar("ResultT")
CommandCall = Callable[["CommandScope"], Awaitable[ResultT]]
_OPERATION = re.compile(r"^[a-z][a-z0-9_.:-]{2,127}$")


@dataclass(frozen=True, slots=True)
class CommandScope:
    """Server-derived metadata and transaction-bound handle passed to a domain command."""

    handle: SpaceHandle
    actor: str
    reason: str
    request_id: str
    dry_run: bool
    expected_revision: int | None


class CommandGateway:
    """Apply authorization, idempotency, revision locking and domain-error mapping once."""

    def __init__(self, idempotency: IdempotencyCoordinator) -> None:
        self.idempotency = idempotency

    async def execute(
        self,
        *,
        request: Request[AdminPrincipal, object, State],
        state: State,
        space_slug: str,
        operation: str,
        mutation: MutationContext,
        call: CommandCall[ResultT],
        identity_id: UUID | None = None,
        payload: Mapping[str, Any] | None = None,
        transaction_mode: Literal["atomic", "external"] = "atomic",
        sensitive: bool = False,
        retain_result: bool = True,
        one_time_result: bool = False,
    ) -> ResultT:
        if not _OPERATION.fullmatch(operation):
            raise ValueError("invalid command operation name")
        resolved = await resolve_space_context(request, state, space_slug)
        if resolved.context.principal.role == "viewer":
            raise HTTPException("operator role is required", status_code=403)
        if mutation.expected_revision is not None and identity_id is None:
            raise HTTPException("expected_revision requires identity_id", status_code=422)
        mutation_payload: object = mutation.model_dump(mode="json")
        fingerprint_payload = {
            "mutation": request_fingerprint(mutation_payload) if sensitive else mutation_payload,
            "identity_id": str(identity_id) if identity_id is not None else None,
            "payload": dict(payload or {}),
        }

        async def invoke() -> ResultT:
            try:
                return await self._invoke(
                    resolved.handle,
                    resolved.context,
                    mutation,
                    call,
                    identity_id,
                    transaction_mode,
                )
            except IdentityHubError as exc:
                raise _domain_error(exc) from None

        return await self.idempotency.execute(
            scope=resolved.context.idempotency_scope,
            operation=operation,
            key=resolved.context.idempotency_key,
            payload=fingerprint_payload,
            call=invoke,
            retain_result=retain_result,
            one_time_result=one_time_result,
        )

    async def _invoke(
        self,
        handle: SpaceHandle,
        context: AdminCommandContext,
        mutation: MutationContext,
        call: CommandCall[ResultT],
        identity_id: UUID | None,
        transaction_mode: Literal["atomic", "external"] = "atomic",
    ) -> ResultT:
        store = handle.hub.store
        if isinstance(store, PgStore) and transaction_mode == "atomic":
            async with store.sessionmaker() as session:
                transaction = await session.begin()
                try:
                    bound = handle.using(session)
                    await _check_revision(bound, identity_id, mutation.expected_revision)
                    result = await call(_scope(bound, context, mutation))
                    if mutation.dry_run:
                        await transaction.rollback()
                    else:
                        await transaction.commit()
                    return result
                except BaseException:
                    if transaction.is_active:
                        await transaction.rollback()
                    raise

        await _check_revision(handle, identity_id, mutation.expected_revision)
        return await call(_scope(handle, context, mutation))


def _scope(handle: SpaceHandle, context: AdminCommandContext, mutation: MutationContext) -> CommandScope:
    return CommandScope(
        handle=handle,
        actor=context.actor,
        reason=mutation.reason,
        request_id=context.request_id,
        dry_run=mutation.dry_run,
        expected_revision=mutation.expected_revision,
    )


async def _check_revision(
    handle: SpaceHandle, identity_id: UUID | None, expected_revision: int | None
) -> None:
    if identity_id is None or expected_revision is None:
        return
    async with handle._tx() as tx:
        await tx.lock_identity_revision(identity_id, expected_revision)


def _domain_error(exc: IdentityHubError) -> HTTPException:
    if isinstance(exc, ConcurrentModification):
        return HTTPException(
            "object revision is stale",
            status_code=412,
            extra={
                "expected_revision": exc.expected_version,
                "current_revision": exc.actual_version,
            },
        )
    if isinstance(exc, NotFound):
        return HTTPException("object not found", status_code=404)
    if isinstance(exc, ValidationFailed):
        return HTTPException(str(exc), status_code=422)
    if isinstance(exc, QuotaExhausted):
        return HTTPException(
            "command rate limit exceeded",
            status_code=429,
            extra={"retry_at": exc.retry_at.isoformat() if exc.retry_at is not None else None},
        )
    if isinstance(
        exc,
        (
            AlreadyExists,
            ChallengeAlreadyOpen,
            LeaseUnavailable,
            OperationNotAllowed,
            TransitionNotAllowed,
            VerificationRequirementsNotMet,
        ),
    ):
        return HTTPException(str(exc), status_code=409)
    return HTTPException("command was rejected", status_code=409)
