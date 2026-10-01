"""Space-scoped command context derived only from authenticated server state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from identity_hub.errors import NotFound
from identity_hub.hub import SpaceHandle
from litestar import Request
from litestar.datastructures import State
from litestar.exceptions import HTTPException, NotFoundException
from pydantic import ValidationError

from identity_hub_admin.api.contracts import MutationHeaders
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.runtime import AdminRuntime


@dataclass(frozen=True, slots=True)
class AdminCommandContext:
    principal: AdminPrincipal
    actor: str
    request_id: str
    space_slug: str
    idempotency_key: str

    @property
    def idempotency_scope(self) -> str:
        return f"{self.principal.actor}:{self.space_slug}"


@dataclass(frozen=True, slots=True)
class ResolvedSpace:
    context: AdminCommandContext
    handle: SpaceHandle


async def resolve_space_context(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: str,
    *,
    require_idempotency: bool = True,
) -> ResolvedSpace:
    principal = request.user
    if not isinstance(principal, AdminPrincipal):
        raise RuntimeError("authentication guard did not provide a principal")
    if not principal.can_access_space(space_slug):
        raise NotFoundException("space not found")
    runtime: AdminRuntime = state.runtime
    if runtime.hub is None:
        raise HTTPException("Identity Hub is unavailable", status_code=503)
    space = getattr(request.state, "space_handle", None)
    if not isinstance(space, SpaceHandle):
        space = await runtime.hub.get_space(space_slug)
        if space is None:
            raise NotFoundException("space not found")
    request_id = str(request.state.request_id)
    idempotency_key = ""
    if require_idempotency:
        try:
            headers = MutationHeaders.model_validate(
                {"Idempotency-Key": request.headers.get("Idempotency-Key")}
            )
        except ValidationError:
            raise HTTPException("valid Idempotency-Key is required", status_code=422) from None
        idempotency_key = headers.idempotency_key
    return ResolvedSpace(
        context=AdminCommandContext(
            principal=principal,
            actor=principal.actor,
            request_id=request_id,
            space_slug=space_slug,
            idempotency_key=idempotency_key,
        ),
        handle=space,
    )


async def ensure_identity_in_space(space: SpaceHandle, identity_id: UUID) -> Any:
    """Resolve an identity through the scoped public read model with a non-enumerating 404."""
    try:
        return await space.identities.card(identity_id)
    except NotFound:
        raise NotFoundException("object not found") from None
