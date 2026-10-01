"""Guarded notification and webhook administration commands."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Annotated, Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from identity_hub.enums import Severity
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
from identity_hub_admin.auth import AdminPrincipal
from identity_hub_admin.commands import CommandGateway, CommandScope

SecretInput = Annotated[
    SecretStr, Field(min_length=1, max_length=4096, json_schema_extra={"writeOnly": True})
]
EventTypes = Annotated[list[str], Field(min_length=1, max_length=100)]


class ChannelCreateCommand(MutationContext):
    title: str = Field(min_length=1, max_length=200)
    bot_token: SecretInput
    chat_id: int | str
    thread_id: int | None = None


class ChannelUpdateCommand(MutationContext):
    title: str = Field(min_length=1, max_length=200)
    bot_token: SecretStr | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        json_schema_extra={"writeOnly": True},
    )
    chat_id: int | str
    thread_id: int | None = None
    enabled: bool


class RuleCreateCommand(MutationContext):
    name: str = Field(min_length=1, max_length=200)
    channel_id: UUID
    event_types: EventTypes
    filter: dict[str, Any] = Field(default_factory=dict)
    min_severity: Literal["info", "warning", "critical"] = "warning"
    digest_seconds: int | None = Field(default=None, ge=10, le=86400)
    template: str | None = Field(default=None, min_length=1, max_length=4096)


class RuleUpdateCommand(MutationContext):
    name: str = Field(min_length=1, max_length=200)
    enabled: bool
    event_types: EventTypes
    filter: dict[str, Any] = Field(default_factory=dict)
    min_severity: Literal["info", "warning", "critical"]
    digest_seconds: int | None = Field(default=None, ge=10, le=86400)
    template: str | None = Field(default=None, min_length=1, max_length=4096)


class WebhookCreateCommand(MutationContext):
    url: str = Field(min_length=8, max_length=2048)
    description: str | None = Field(default=None, max_length=1000)
    event_types: EventTypes
    filter: dict[str, Any] = Field(default_factory=dict)
    min_severity: Literal["info", "warning", "critical"] = "info"


class WebhookUpdateCommand(WebhookCreateCommand):
    pass


class EnableCommand(MutationContext):
    enabled: bool


class TestNotificationCommand(MutationContext):
    text: str = Field(default="Identity Hub: тестовое уведомление", min_length=1, max_length=4096)


class TestWebhookCommand(MutationContext):
    pass


class DeleteCommand(MutationContext):
    confirmation: Confirmation

    @model_validator(mode="after")
    def validate_confirmation(self) -> DeleteCommand:
        if "deletes_integration" not in self.confirmation.consequence_codes:
            raise ValueError("integration deletion requires deletes_integration acknowledgement")
        return self


class RotateWebhookCommand(MutationContext):
    overlap_seconds: int = Field(default=3600, ge=0, le=86400)
    confirmation: Confirmation

    @model_validator(mode="after")
    def validate_confirmation(self) -> RotateWebhookCommand:
        if "rotates_signing_key" not in self.confirmation.consequence_codes:
            raise ValueError("rotation requires rotates_signing_key acknowledgement")
        return self


class RetryDeliveryCommand(MutationContext):
    confirmation: Confirmation

    @model_validator(mode="after")
    def validate_confirmation(self) -> RetryDeliveryCommand:
        if "retries_delivery" not in self.confirmation.consequence_codes:
            raise ValueError("manual retry requires retries_delivery acknowledgement")
        return self


class IntegrationCommandResponse(ContractModel):
    action: str
    resource_kind: str
    resource_id: str
    outcome: str = "changed"


class OneTimeSigningValueResponse(IntegrationCommandResponse):
    one_time_signing_value: str
    overlap_seconds: int | None = None


class WebhookTestResponse(IntegrationCommandResponse):
    response_code: int


_NO_STORE = {
    "Cache-Control": "no-store, private, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _gateway(state: State) -> CommandGateway:
    return cast(CommandGateway, state.command_gateway)


async def _audit(
    scope: CommandScope,
    action: str,
    kind: Literal["notification_channel", "notification_rule", "webhook_endpoint", "delivery"],
    resource_id: UUID | int,
    *,
    outcome: str = "changed",
) -> None:
    await scope.handle.admin.record_integration(
        action,
        kind,
        str(resource_id),
        actor=scope.actor,
        reason=scope.reason,
        outcome=outcome,
    )


def _result(
    action: str, kind: str, resource_id: UUID | int, outcome: str = "changed"
) -> ResourceEnvelope[IntegrationCommandResponse]:
    return ResourceEnvelope(
        data=IntegrationCommandResponse(
            action=action,
            resource_kind=kind,
            resource_id=str(resource_id),
            outcome=outcome,
        )
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-channels",
    operation_id="admin_v1_notification_channel_create_post",
    tags=["notifications"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 409, 422),
)
async def create_channel(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    data: ChannelCreateCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        channel = await scope.handle.notifications.create_telegram_channel(
            data.title,
            bot_token=data.bot_token,
            chat_id=data.chat_id,
            thread_id=data.thread_id,
        )
        await _audit(scope, "create", "notification_channel", channel.id)
        return _result("create", "notification_channel", channel.id)

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.channel.create",
        mutation=data,
        call=apply,
        sensitive=True,
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-channels/{channel_id:uuid}",
    operation_id="admin_v1_notification_channel_update_post",
    tags=["notifications"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def update_channel(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    channel_id: FromPath[UUID],
    data: ChannelUpdateCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.update_telegram_channel(
            channel_id,
            title=data.title,
            bot_token=data.bot_token,
            chat_id=data.chat_id,
            thread_id=data.thread_id,
            enabled=data.enabled,
        )
        await _audit(scope, "update", "notification_channel", channel_id)
        return _result("update", "notification_channel", channel_id)

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.channel.update",
        mutation=data,
        call=apply,
        payload={"channel_id": str(channel_id)},
        sensitive=True,
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-channels/{channel_id:uuid}/commands/delete",
    operation_id="admin_v1_notification_channel_delete_post",
    tags=["notifications"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def delete_channel(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    channel_id: FromPath[UUID],
    data: DeleteCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.delete_channel(channel_id)
        await _audit(scope, "delete", "notification_channel", channel_id)
        return _result("delete", "notification_channel", channel_id, "deleted")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.channel.delete",
        mutation=data,
        call=apply,
        payload={"channel_id": str(channel_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-channels/{channel_id:uuid}/commands/test",
    operation_id="admin_v1_notification_channel_test_post",
    tags=["notifications"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
async def test_channel(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    channel_id: FromPath[UUID],
    data: TestNotificationCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.send_test(channel_id, data.text)
        await _audit(scope, "test", "notification_channel", channel_id, outcome="sent")
        return _result("test", "notification_channel", channel_id, "sent")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.channel.test",
        mutation=data,
        call=apply,
        payload={"channel_id": str(channel_id)},
        transaction_mode="external",
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-channels/{channel_id:uuid}/commands/enable",
    operation_id="admin_v1_notification_channel_enable_post",
    tags=["notifications"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def enable_channel(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    channel_id: FromPath[UUID],
    data: EnableCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.update_telegram_channel(channel_id, enabled=data.enabled)
        action = "enable" if data.enabled else "disable"
        await _audit(scope, action, "notification_channel", channel_id)
        return _result(action, "notification_channel", channel_id, action + "d")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.channel.enable",
        mutation=data,
        call=apply,
        payload={"channel_id": str(channel_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-rules",
    operation_id="admin_v1_notification_rule_create_post",
    tags=["notifications"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def create_rule(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    data: RuleCreateCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        rule = await scope.handle.notifications.create_rule(
            name=data.name,
            channel_id=data.channel_id,
            event_types=data.event_types,
            filter=data.filter,
            min_severity=Severity(data.min_severity),
            digest_seconds=data.digest_seconds,
            template=data.template,
            created_by=scope.actor,
        )
        await _audit(scope, "create", "notification_rule", rule.id)
        return _result("create", "notification_rule", rule.id)

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.rule.create",
        mutation=data,
        call=apply,
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-rules/{rule_id:uuid}",
    operation_id="admin_v1_notification_rule_update_post",
    tags=["notifications"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def update_rule(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    rule_id: FromPath[UUID],
    data: RuleUpdateCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.update_rule(
            rule_id,
            name=data.name,
            enabled=data.enabled,
            event_types=data.event_types,
            filter=data.filter,
            min_severity=Severity(data.min_severity),
            digest_seconds=data.digest_seconds,
            template=data.template,
        )
        await _audit(scope, "update", "notification_rule", rule_id)
        return _result("update", "notification_rule", rule_id)

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.rule.update",
        mutation=data,
        call=apply,
        payload={"rule_id": str(rule_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-rules/{rule_id:uuid}/commands/delete",
    operation_id="admin_v1_notification_rule_delete_post",
    tags=["notifications"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def delete_rule(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    rule_id: FromPath[UUID],
    data: DeleteCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.delete_rule(rule_id)
        await _audit(scope, "delete", "notification_rule", rule_id)
        return _result("delete", "notification_rule", rule_id, "deleted")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.rule.delete",
        mutation=data,
        call=apply,
        payload={"rule_id": str(rule_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-rules/{rule_id:uuid}/commands/enable",
    operation_id="admin_v1_notification_rule_enable_post",
    tags=["notifications"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def enable_rule(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    rule_id: FromPath[UUID],
    data: EnableCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.update_rule(rule_id, enabled=data.enabled)
        action = "enable" if data.enabled else "disable"
        await _audit(scope, action, "notification_rule", rule_id)
        return _result(action, "notification_rule", rule_id, action + "d")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.rule.enable",
        mutation=data,
        call=apply,
        payload={"rule_id": str(rule_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/webhook-endpoints",
    operation_id="admin_v1_webhook_endpoint_create_post",
    tags=["webhooks"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 409, 422),
)
async def create_webhook(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    data: WebhookCreateCommand,
) -> Response[ResourceEnvelope[OneTimeSigningValueResponse]]:
    endpoint_id = uuid5(
        NAMESPACE_URL,
        f"{request.user.actor}:{space_slug}:{request.headers.get('Idempotency-Key', '')}:webhook",
    )

    async def apply(scope: CommandScope) -> Response[ResourceEnvelope[OneTimeSigningValueResponse]]:
        value = secrets.token_urlsafe(32)
        endpoint = await scope.handle.webhooks.create(
            id=endpoint_id,
            url=data.url,
            secret=SecretStr(value),
            description=data.description,
            event_types=data.event_types,
            filter=data.filter,
            min_severity=Severity(data.min_severity),
        )
        await _audit(scope, "create", "webhook_endpoint", endpoint.id)
        return Response(
            ResourceEnvelope(
                data=OneTimeSigningValueResponse(
                    action="create",
                    resource_kind="webhook_endpoint",
                    resource_id=str(endpoint.id),
                    one_time_signing_value=value,
                )
            ),
            headers=_NO_STORE,
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="webhook.endpoint.create",
        mutation=data,
        call=apply,
        sensitive=True,
        one_time_result=True,
    )


@post(
    "/api/v1/spaces/{space_slug:str}/webhook-endpoints/{endpoint_id:uuid}",
    operation_id="admin_v1_webhook_endpoint_update_post",
    tags=["webhooks"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def update_webhook(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    endpoint_id: FromPath[UUID],
    data: WebhookUpdateCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.webhooks.update(
            endpoint_id,
            url=data.url,
            description=data.description,
            event_types=data.event_types,
            filter=data.filter,
            min_severity=Severity(data.min_severity),
        )
        await _audit(scope, "update", "webhook_endpoint", endpoint_id)
        return _result("update", "webhook_endpoint", endpoint_id)

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="webhook.endpoint.update",
        mutation=data,
        call=apply,
        payload={"endpoint_id": str(endpoint_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/webhook-endpoints/{endpoint_id:uuid}/commands/enable",
    operation_id="admin_v1_webhook_endpoint_enable_post",
    tags=["webhooks"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def enable_webhook(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    endpoint_id: FromPath[UUID],
    data: EnableCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.webhooks.set_enabled(
            endpoint_id, data.enabled, reason=None if data.enabled else scope.reason
        )
        action = "enable" if data.enabled else "disable"
        await _audit(scope, action, "webhook_endpoint", endpoint_id)
        return _result(action, "webhook_endpoint", endpoint_id, "enabled" if data.enabled else "disabled")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="webhook.endpoint.enable",
        mutation=data,
        call=apply,
        payload={"endpoint_id": str(endpoint_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/webhook-endpoints/{endpoint_id:uuid}/commands/rotate",
    operation_id="admin_v1_webhook_endpoint_rotate_post",
    tags=["webhooks"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def rotate_webhook(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    endpoint_id: FromPath[UUID],
    data: RotateWebhookCommand,
) -> Response[ResourceEnvelope[OneTimeSigningValueResponse]]:
    async def apply(scope: CommandScope) -> Response[ResourceEnvelope[OneTimeSigningValueResponse]]:
        value = secrets.token_urlsafe(32)
        await scope.handle.webhooks.rotate_secret(
            endpoint_id, SecretStr(value), overlap=timedelta(seconds=data.overlap_seconds)
        )
        await _audit(scope, "rotate", "webhook_endpoint", endpoint_id)
        return Response(
            ResourceEnvelope(
                data=OneTimeSigningValueResponse(
                    action="rotate",
                    resource_kind="webhook_endpoint",
                    resource_id=str(endpoint_id),
                    one_time_signing_value=value,
                    overlap_seconds=data.overlap_seconds,
                )
            ),
            headers=_NO_STORE,
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="webhook.endpoint.rotate",
        mutation=data,
        call=apply,
        payload={"endpoint_id": str(endpoint_id)},
        sensitive=True,
        one_time_result=True,
    )


@post(
    "/api/v1/spaces/{space_slug:str}/webhook-endpoints/{endpoint_id:uuid}/commands/test",
    operation_id="admin_v1_webhook_endpoint_test_post",
    tags=["webhooks"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422, 503),
)
async def test_webhook(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    endpoint_id: FromPath[UUID],
    data: TestWebhookCommand,
) -> ResourceEnvelope[WebhookTestResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[WebhookTestResponse]:
        code = await scope.handle.webhooks.send_test(endpoint_id)
        await _audit(scope, "test", "webhook_endpoint", endpoint_id, outcome=f"http_{code}")
        return ResourceEnvelope(
            data=WebhookTestResponse(
                action="test",
                resource_kind="webhook_endpoint",
                resource_id=str(endpoint_id),
                outcome="sent",
                response_code=code,
            )
        )

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="webhook.endpoint.test",
        mutation=data,
        call=apply,
        payload={"endpoint_id": str(endpoint_id)},
        transaction_mode="external",
    )


@post(
    "/api/v1/spaces/{space_slug:str}/webhook-endpoints/{endpoint_id:uuid}/commands/delete",
    operation_id="admin_v1_webhook_endpoint_delete_post",
    tags=["webhooks"],
    opt=route_meta("admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def delete_webhook(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    endpoint_id: FromPath[UUID],
    data: DeleteCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.webhooks.delete(endpoint_id)
        await _audit(scope, "delete", "webhook_endpoint", endpoint_id)
        return _result("delete", "webhook_endpoint", endpoint_id, "deleted")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="webhook.endpoint.delete",
        mutation=data,
        call=apply,
        payload={"endpoint_id": str(endpoint_id)},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/webhook-deliveries/{delivery_id:int}/commands/retry",
    operation_id="admin_v1_webhook_delivery_retry_post",
    tags=["webhooks"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def retry_webhook_delivery(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    delivery_id: FromPath[int],
    data: RetryDeliveryCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.webhooks.retry(delivery_id)
        await _audit(scope, "retry", "delivery", delivery_id, outcome="queued")
        return _result("retry", "delivery", delivery_id, "queued")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="webhook.delivery.retry",
        mutation=data,
        call=apply,
        payload={"delivery_id": delivery_id},
    )


@post(
    "/api/v1/spaces/{space_slug:str}/notification-deliveries/{delivery_id:int}/commands/retry",
    operation_id="admin_v1_notification_delivery_retry_post",
    tags=["notifications"],
    opt=route_meta("operator", "admin", space_scoped=True),
    responses=error_responses(401, 403, 404, 409, 422),
)
async def retry_notification_delivery(
    request: Request[AdminPrincipal, object, State],
    state: State,
    space_slug: FromPath[str],
    delivery_id: FromPath[int],
    data: RetryDeliveryCommand,
) -> ResourceEnvelope[IntegrationCommandResponse]:
    async def apply(scope: CommandScope) -> ResourceEnvelope[IntegrationCommandResponse]:
        await scope.handle.notifications.retry(delivery_id)
        await _audit(scope, "retry", "delivery", delivery_id, outcome="queued")
        return _result("retry", "delivery", delivery_id, "queued")

    return await _gateway(state).execute(
        request=request,
        state=state,
        space_slug=space_slug,
        operation="notification.delivery.retry",
        mutation=data,
        call=apply,
        payload={"delivery_id": delivery_id},
    )


INTEGRATION_COMMAND_ROUTE_HANDLERS = [
    create_channel,
    update_channel,
    delete_channel,
    test_channel,
    enable_channel,
    create_rule,
    update_rule,
    delete_rule,
    enable_rule,
    create_webhook,
    update_webhook,
    enable_webhook,
    rotate_webhook,
    test_webhook,
    delete_webhook,
    retry_webhook_delivery,
    retry_notification_delivery,
]
