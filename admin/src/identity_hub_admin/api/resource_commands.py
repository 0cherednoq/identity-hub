"""Guarded lease, proxy and account-link administration commands."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, cast
from uuid import UUID

from identity_hub.enums import ProxyStatus
from identity_hub.errors import ValidationFailed
from identity_hub.proxies import ProxyChecker
from litestar import Request, post
from litestar.datastructures import State
from litestar.exceptions import HTTPException
from litestar.params import FromPath
from pydantic import Field

from identity_hub_admin.api.contracts import Confirmation, ContractModel, MutationContext, ResourceEnvelope
from identity_hub_admin.api.routes import error_responses, route_meta
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.commands import CommandGateway, CommandScope


class LeaseRevokeCommand(MutationContext):
    confirmation: Confirmation


class ProxyRebindCommand(MutationContext):
    proxy_id: UUID
    confirmation: Confirmation


class LinkAccountCommand(MutationContext):
    role: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    linked_identity_id: UUID


class UnlinkAccountCommand(MutationContext):
    role: str = Field(min_length=1, max_length=64)
    confirmation: Confirmation | None = None


class ProxyRecheckCommand(MutationContext):
    pass


class ProxyOverrideCommand(MutationContext):
    status: Literal["alive", "quarantined", "dead"]
    pin_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    confirmation: Confirmation


class ProxyDisableCommand(MutationContext):
    unbind: bool = False
    confirmation: Confirmation


class ProxyEnableCommand(MutationContext):
    pass


class ResourceCommandResponse(ContractModel):
    action: Literal[
        "lease_revoke",
        "proxy_rebind",
        "link",
        "unlink",
        "proxy_recheck",
        "proxy_override",
        "proxy_disable",
        "proxy_enable",
    ]
    changed: bool
    outcome: str
    affected_identity_ids: list[UUID] = Field(default_factory=list)


def _gateway(state: State) -> CommandGateway:
    return cast(CommandGateway, state.command_gateway)


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/lease-revoke",
    operation_id="admin_v1_account_lease_revoke_post",
    tags=["resource-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def revoke_lease_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: LeaseRevokeCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        changed = await scope.handle.pools.revoke(identity_id, reason=scope.reason, actor=scope.actor)
        return ResourceEnvelope(
            data=ResourceCommandResponse(
                action="lease_revoke", changed=changed, outcome="revoked" if changed else "no_live_lease"
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.lease.revoke",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        transaction_mode="external",
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/proxy-rebind",
    operation_id="admin_v1_account_proxy_rebind_post",
    tags=["resource-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def rebind_proxy_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: ProxyRebindCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        changed = await scope.handle.proxies.rebind(
            identity_id, data.proxy_id, reason=scope.reason, actor=scope.actor
        )
        return ResourceEnvelope(
            data=ResourceCommandResponse(
                action="proxy_rebind", changed=changed, outcome="rebound" if changed else "unchanged"
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.proxy.rebind",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"proxy_id": str(data.proxy_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/link",
    operation_id="admin_v1_account_link_post",
    tags=["resource-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def link_account_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: LinkAccountCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        await scope.handle.links.add(
            identity_id, data.role, data.linked_identity_id, actor=scope.actor, reason=scope.reason
        )
        return ResourceEnvelope(data=ResourceCommandResponse(action="link", changed=True, outcome="linked"))

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.link.create",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"role": data.role, "linked_identity_id": str(data.linked_identity_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/accounts/{identity_id:uuid}/commands/unlink",
    operation_id="admin_v1_account_unlink_post",
    tags=["resource-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 412, 422),
)
async def unlink_account_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    identity_id: FromPath[UUID],
    data: UnlinkAccountCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        impact = await scope.handle.links.impact(identity_id)
        if impact and (
            data.confirmation is None or "affects_dependents" not in data.confirmation.consequence_codes
        ):
            raise ValidationFailed("unlink affects dependent accounts and requires explicit confirmation")
        changed = await scope.handle.links.remove(
            identity_id, data.role, actor=scope.actor, reason=scope.reason
        )
        return ResourceEnvelope(
            data=ResourceCommandResponse(
                action="unlink",
                changed=changed,
                outcome="unlinked" if changed else "not_linked",
                affected_identity_ids=[item.identity_id for item in impact],
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="identity.link.delete",
        mutation=data,
        call=apply,
        identity_id=identity_id,
        payload={"role": data.role},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/proxies/{proxy_id:uuid}/commands/recheck",
    operation_id="admin_v1_proxy_recheck_post",
    tags=["resource-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
async def recheck_proxy_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    proxy_id: FromPath[UUID],
    data: ProxyRecheckCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    checker = cast(ProxyChecker | None, getattr(state, "proxy_checker", None))
    if checker is None:
        raise HTTPException("proxy checker is not configured", status_code=503)

    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        status = await scope.handle.proxies.recheck(proxy_id, checker, actor=scope.actor)
        return ResourceEnvelope(
            data=ResourceCommandResponse(action="proxy_recheck", changed=True, outcome=status)
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="proxy.health.recheck",
        mutation=data,
        call=apply,
        transaction_mode="external",
        payload={"proxy_id": str(proxy_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/proxies/{proxy_id:uuid}/commands/override",
    operation_id="admin_v1_proxy_override_post",
    tags=["resource-commands"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def override_proxy_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    proxy_id: FromPath[UUID],
    data: ProxyOverrideCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        status = await scope.handle.proxies.override(
            proxy_id,
            ProxyStatus(data.status),
            reason=scope.reason,
            actor=scope.actor,
            pin_for=timedelta(seconds=data.pin_seconds) if data.pin_seconds is not None else None,
        )
        return ResourceEnvelope(
            data=ResourceCommandResponse(action="proxy_override", changed=True, outcome=status)
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="proxy.health.override",
        mutation=data,
        call=apply,
        payload={"proxy_id": str(proxy_id), "status": data.status},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/proxies/{proxy_id:uuid}/commands/disable",
    operation_id="admin_v1_proxy_disable_post",
    tags=["resource-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def disable_proxy_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    proxy_id: FromPath[UUID],
    data: ProxyDisableCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    """Выключить прокси: он не выдаётся, статус здоровья и привязки сохраняются."""

    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        before = await scope.handle.proxies.get(proxy_id)
        await scope.handle.proxies.disable(
            proxy_id, reason=scope.reason, actor=scope.actor, unbind=data.unbind
        )
        return ResourceEnvelope(
            data=ResourceCommandResponse(
                action="proxy_disable",
                changed=before.enabled,
                outcome="disabled" if before.enabled else "unchanged",
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="proxy.enabled.disable",
        mutation=data,
        call=apply,
        payload={"proxy_id": str(proxy_id), "unbind": data.unbind},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/proxies/{proxy_id:uuid}/commands/enable",
    operation_id="admin_v1_proxy_enable_post",
    tags=["resource-commands"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def enable_proxy_command(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    proxy_id: FromPath[UUID],
    data: ProxyEnableCommand,
) -> ResourceEnvelope[ResourceCommandResponse]:
    """Включить прокси с прежним статусом здоровья."""

    async def apply(scope: CommandScope) -> ResourceEnvelope[ResourceCommandResponse]:
        before = await scope.handle.proxies.get(proxy_id)
        await scope.handle.proxies.enable(proxy_id, actor=scope.actor)
        return ResourceEnvelope(
            data=ResourceCommandResponse(
                action="proxy_enable",
                changed=not before.enabled,
                outcome="unchanged" if before.enabled else "enabled",
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="proxy.enabled.enable",
        mutation=data,
        call=apply,
        payload={"proxy_id": str(proxy_id)},
    )


RESOURCE_COMMAND_ROUTE_HANDLERS = [
    revoke_lease_command,
    rebind_proxy_command,
    link_account_command,
    unlink_account_command,
    recheck_proxy_command,
    override_proxy_command,
    disable_proxy_command,
    enable_proxy_command,
]
