"""Read-only Telegram notification and webhook delivery center."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from litestar import Request, get
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import FromPath, Parameter

from identity_hub_admin.api.contracts import (
    ContractModel,
    NumericCursorPayload,
    ResourceEnvelope,
    decode_numeric_cursor,
    encode_numeric_cursor,
)
from identity_hub_admin.api.routes import error_responses, route_meta


class NotificationChannelResponse(ContractModel):
    id: UUID
    title: str
    kind: str
    masked_target: str
    thread_id: int | None
    enabled: bool
    created_at: datetime
    actions_available: bool
    actions_unavailable_reason: str


class NotificationRuleResponse(ContractModel):
    id: UUID
    channel_id: UUID
    name: str
    enabled: bool
    event_types: list[str]
    min_severity: str
    filter: dict[str, Any]
    digest_seconds: int | None
    template_configured: bool
    preview_24h: int
    updated_at: datetime
    actions_available: bool
    actions_unavailable_reason: str


class WebhookEndpointResponse(ContractModel):
    id: UUID
    masked_target: str
    description: str | None
    event_types: list[str]
    filter: dict[str, Any]
    min_severity: str
    enabled: bool
    disabled_reason: str | None
    failure_streak: int
    created_at: datetime
    actions_available: bool
    actions_unavailable_reason: str


class WebhookDeliveryResponse(ContractModel):
    id: int
    endpoint_id: UUID
    event_id: UUID
    status: str
    attempt: int
    next_attempt_at: datetime
    response_code: int | None
    response_ms: int | None
    has_error: bool
    error_summary: str | None
    created_at: datetime
    delivered_at: datetime | None


class NotificationDeliveryResponse(ContractModel):
    id: int
    rule_id: UUID
    rule_name: str
    channel_id: UUID
    channel_title: str
    event_ids: list[UUID]
    event_count: int
    status: str
    attempt: int
    scheduled_at: datetime
    sent_at: datetime | None
    has_error: bool
    error_summary: str | None


class DeliveryRetryPolicyResponse(ContractModel):
    base_seconds: int
    cap_seconds: int
    max_attempts: int
    jitter_ratio: float


class DeliveryCenterResponse(ContractModel):
    channels: list[NotificationChannelResponse]
    rules: list[NotificationRuleResponse]
    webhook_endpoints: list[WebhookEndpointResponse]
    webhook_deliveries: list[WebhookDeliveryResponse]
    webhook_next_cursor: str | None
    notifications: list[NotificationDeliveryResponse]
    notification_next_cursor: str | None
    preview_window_started_at: datetime
    preview_mutates_state: Literal[False] = False
    retry_policy: DeliveryRetryPolicyResponse


def _handle(request: Request[Any, Any, Any], space_slug: str) -> SpaceHandle:
    handle = getattr(request.state, "space_handle", None)
    if handle is None or handle.slug != space_slug:
        raise NotFoundException("space not found")
    return cast(SpaceHandle, handle)


def _cursor(value: str | None, sort: str) -> tuple[datetime, int] | None:
    if value is None:
        return None
    try:
        payload = decode_numeric_cursor(value)
        if payload.sort != sort:
            raise ValueError("invalid delivery cursor sort")
        at = datetime.fromisoformat(payload.value)
        if at.tzinfo is None:
            raise ValueError("delivery cursor datetime must include timezone")
        return at, payload.id
    except ValueError as exc:
        raise HTTPException(str(exc), status_code=400) from None


def _next_cursor(value: tuple[datetime, int] | None, sort: str) -> str | None:
    if value is None:
        return None
    return encode_numeric_cursor(NumericCursorPayload(sort=sort, value=value[0].isoformat(), id=value[1]))


@get(
    "/api/v1/spaces/{space_slug:str}/delivery-center",
    operation_id="admin_v1_delivery_center_get",
    tags=["notifications", "webhooks"],
    opt=route_meta("viewer", "operator", "admin", space_scoped=True),
    responses=error_responses(400, 401, 403, 404, 503),
)
async def delivery_center(
    request: Request[Any, Any, Any],
    space_slug: FromPath[str],
    webhook_cursor: str | None = Parameter(default=None, max_length=2048),
    webhook_limit: int = Parameter(default=50, ge=1, le=200),
    notification_cursor: str | None = Parameter(default=None, max_length=2048),
    notification_limit: int = Parameter(default=50, ge=1, le=200),
) -> ResourceEnvelope[DeliveryCenterResponse]:
    try:
        handle = _handle(request, space_slug)
        center = await handle.delivery_center(
            webhook_limit=webhook_limit,
            webhook_before=_cursor(webhook_cursor, "created_at"),
            notification_limit=notification_limit,
            notification_before=_cursor(notification_cursor, "scheduled_at"),
        )
    except ValidationFailed as exc:
        raise HTTPException(str(exc), status_code=400) from None
    actions_available = getattr(request.user, "role", "viewer") != "viewer"
    actions_reason = "" if actions_available else "operator role is required"
    return ResourceEnvelope(
        data=DeliveryCenterResponse(
            channels=[
                NotificationChannelResponse.model_validate(item, from_attributes=True).model_copy(
                    update={
                        "actions_available": actions_available,
                        "actions_unavailable_reason": actions_reason,
                    }
                )
                for item in center.channels
            ],
            rules=[
                NotificationRuleResponse.model_validate(item, from_attributes=True).model_copy(
                    update={
                        "actions_available": actions_available,
                        "actions_unavailable_reason": actions_reason,
                    }
                )
                for item in center.rules
            ],
            webhook_endpoints=[
                WebhookEndpointResponse.model_validate(item, from_attributes=True).model_copy(
                    update={
                        "actions_available": actions_available,
                        "actions_unavailable_reason": actions_reason,
                    }
                )
                for item in center.webhook_endpoints
            ],
            webhook_deliveries=[
                WebhookDeliveryResponse.model_validate(item, from_attributes=True)
                for item in center.webhook_deliveries.items
            ],
            webhook_next_cursor=_next_cursor(center.webhook_deliveries.next_before, "created_at"),
            notifications=[
                NotificationDeliveryResponse.model_validate(item, from_attributes=True)
                for item in center.notifications.items
            ],
            notification_next_cursor=_next_cursor(center.notifications.next_before, "scheduled_at"),
            preview_window_started_at=center.preview_window_started_at,
            retry_policy=DeliveryRetryPolicyResponse(
                base_seconds=int(handle.hub.webhook_dispatcher.policy.retry_base.total_seconds()),
                cap_seconds=int(handle.hub.webhook_dispatcher.policy.retry_cap.total_seconds()),
                max_attempts=handle.hub.webhook_dispatcher.policy.max_failures,
                jitter_ratio=handle.hub.webhook_dispatcher.policy.retry_jitter,
            ),
        )
    )


DELIVERY_ROUTE_HANDLERS = [delivery_center]
