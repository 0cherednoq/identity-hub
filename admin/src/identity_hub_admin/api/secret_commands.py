"""Admin-only, non-cacheable account secret reveal and update endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from litestar import Request, Response, post
from litestar.datastructures import State
from litestar.params import FromPath
from pydantic import Field, SecretStr, model_validator

from identity_hub_admin.api.contracts import (
    Confirmation,
    ContractModel,
    MutationContext,
    ResourceEnvelope,
)
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal, AuthService
from identity_hub_admin.commands import CommandGateway, CommandScope


class RevealSecretCommand(MutationContext):
    field: str = Field(
        min_length=1,
        max_length=193,
        pattern=r"^(credentials|profile|session(?::[A-Za-z0-9_.:-]{1,64})?|evidence:[A-Za-z0-9_.:-]{1,128})$",
    )
    reauthentication: SecretStr = Field(min_length=1, max_length=4096)
    confirmation: Confirmation

    @model_validator(mode="after")
    def validate_confirmation(self) -> RevealSecretCommand:
        if "reveals_secret" not in self.confirmation.consequence_codes:
            raise ValueError("secret reveal requires reveals_secret acknowledgement")
        return self


class UpdateSecretCommand(MutationContext):
    kind: Literal["credentials", "session"]
    value: dict[str, Any]
    slot: str = Field(default="default", min_length=1, max_length=64)
    expires_at: datetime | None = None
    reauthentication: SecretStr = Field(min_length=1, max_length=4096)
    confirmation: Confirmation

    @model_validator(mode="after")
    def validate_confirmation(self) -> UpdateSecretCommand:
        if "replaces_secret" not in self.confirmation.consequence_codes:
            raise ValueError("secret update requires replaces_secret acknowledgement")
        if self.kind == "credentials" and (self.slot != "default" or self.expires_at is not None):
            raise ValueError("credentials update does not accept session metadata")
        return self


class SecretRevealResponse(ContractModel):
    field: str
    value: dict[str, Any]
    hide_after_seconds: int = 30


class SecretUpdateResponse(ContractModel):
    kind: Literal["credentials", "session"]
    slot: str | None = None
    changed: bool = True


def _gateway(state: State) -> CommandGateway:
    return cast(CommandGateway, state.command_gateway)


def _reauthenticate(request: Request[AdminPrincipal, object, State], state: State, proof: SecretStr) -> None:
    service = cast(AuthService, state.auth_service)
    service.check_reveal_rate(request.user.actor)
    service.verify_reauthentication(request.user, proof.get_secret_value())


_NO_STORE = {
    "Cache-Control": "no-store, private, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/secrets/reveal",
    operation_id="admin_v1_account_secret_reveal_post",
    tags=["account-secrets"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422, 429),
)
async def reveal_secret(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: RevealSecretCommand,
) -> Response[ResourceEnvelope[SecretRevealResponse]]:
    _reauthenticate(request, state, data.reauthentication)

    async def apply(scope: CommandScope) -> Response[ResourceEnvelope[SecretRevealResponse]]:
        value = await scope.handle.reveal_secret(
            identity_id, data.field, actor=scope.actor, reason=scope.reason
        )
        return Response(
            ResourceEnvelope(data=SecretRevealResponse(field=data.field, value=value)),
            headers=_NO_STORE,
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.secret.reveal",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"field": data.field},
        transaction_mode="external",
        sensitive=True,
        retain_result=False,
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/secrets/update",
    operation_id="admin_v1_account_secret_update_post",
    tags=["account-secrets"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422, 429),
)
async def update_secret(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: UpdateSecretCommand,
) -> Response[ResourceEnvelope[SecretUpdateResponse]]:
    _reauthenticate(request, state, data.reauthentication)

    async def apply(scope: CommandScope) -> Response[ResourceEnvelope[SecretUpdateResponse]]:
        if data.kind == "credentials":
            await scope.handle.update_credentials(identity_id, data.value, actor=scope.actor)
            slot = None
        else:
            await scope.handle.update_session(
                identity_id,
                data.value,
                actor=scope.actor,
                slot=data.slot,
                expires_at=data.expires_at,
            )
            slot = data.slot
        return Response(
            ResourceEnvelope(data=SecretUpdateResponse(kind=data.kind, slot=slot)),
            headers=_NO_STORE,
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation=f"identity.secret.{data.kind}.update",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"kind": data.kind, "slot": data.slot},
        sensitive=True,
    )


SECRET_COMMAND_ROUTE_HANDLERS = [reveal_secret, update_secret]
