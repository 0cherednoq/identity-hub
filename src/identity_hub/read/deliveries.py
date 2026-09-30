"""Secret-free notification and webhook administration reads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from identity_hub._space import SpaceBase
from identity_hub.errors import ValidationFailed
from identity_hub.events import CATALOG, matches_any, scrub, severity_at_least
from identity_hub.store.base import EventReadFilter, EventReadRow, NotificationRuleRow

_ACTIONS_REASON = "Actions become available in A3.8."


@dataclass(frozen=True, slots=True)
class NotificationChannelItem:
    id: UUID
    title: str
    kind: str
    masked_target: str
    thread_id: int | None
    enabled: bool
    created_at: datetime
    actions_available: bool = False
    actions_unavailable_reason: str = _ACTIONS_REASON


@dataclass(frozen=True, slots=True)
class NotificationRuleItem:
    id: UUID
    channel_id: UUID
    name: str
    enabled: bool
    event_types: tuple[str, ...]
    min_severity: str
    filter: Mapping[str, Any]
    digest_seconds: int | None
    template_configured: bool
    preview_24h: int
    updated_at: datetime
    actions_available: bool = False
    actions_unavailable_reason: str = _ACTIONS_REASON


@dataclass(frozen=True, slots=True)
class WebhookEndpointItem:
    id: UUID
    masked_target: str
    description: str | None
    event_types: tuple[str, ...]
    filter: Mapping[str, Any]
    min_severity: str
    enabled: bool
    disabled_reason: str | None
    failure_streak: int
    created_at: datetime
    actions_available: bool = False
    actions_unavailable_reason: str = _ACTIONS_REASON


@dataclass(frozen=True, slots=True)
class WebhookDeliveryItem:
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


@dataclass(frozen=True, slots=True)
class NotificationDeliveryItem:
    id: int
    rule_id: UUID
    rule_name: str
    channel_id: UUID
    channel_title: str
    event_ids: tuple[UUID, ...]
    event_count: int
    status: str
    attempt: int
    scheduled_at: datetime
    sent_at: datetime | None
    has_error: bool
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryPage:
    items: tuple[WebhookDeliveryItem, ...]
    next_before: tuple[datetime, int] | None


@dataclass(frozen=True, slots=True)
class NotificationDeliveryPage:
    items: tuple[NotificationDeliveryItem, ...]
    next_before: tuple[datetime, int] | None


@dataclass(frozen=True, slots=True)
class DeliveryCenter:
    channels: tuple[NotificationChannelItem, ...]
    rules: tuple[NotificationRuleItem, ...]
    webhook_endpoints: tuple[WebhookEndpointItem, ...]
    webhook_deliveries: WebhookDeliveryPage
    notifications: NotificationDeliveryPage
    preview_window_started_at: datetime


def _mask_chat_id(value: object) -> str:
    rendered = str(value)
    suffix = rendered[-4:] if len(rendered) > 4 else rendered[-2:]
    prefix = "@" if rendered.startswith("@") else ""
    return f"{prefix}••••{suffix.lstrip('@')}"


def _mask_webhook_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or "unknown"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}/…"


def _disabled_reason(value: str | None) -> str | None:
    if value is None:
        return None
    if value == "consecutive_failures":
        return value
    return "disabled_by_operator"


def _matches_rule(rule: NotificationRuleRow, event: EventReadRow, labels: Mapping[str, Any]) -> bool:
    event_type = event.type
    if not matches_any(rule.event_types, event_type):
        return False
    if not severity_at_least(event.severity, rule.min_severity):
        return False
    data = event.data
    for key, expected in rule.filter.items():
        allowed = expected if isinstance(expected, list | tuple | set) else [expected]
        if key == "service":
            actual: Any = event.service
        elif key == "labels":
            if not isinstance(expected, Mapping):
                return False
            if any(labels.get(str(label)) != value for label, value in expected.items()):
                return False
            continue
        elif key.startswith("data."):
            actual = data.get(key[5:])
        else:
            return False
        if actual not in allowed:
            return False
    return True


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 200:
        raise ValidationFailed("limit must be between 1 and 200")


class DeliveriesReadMixin(SpaceBase):
    async def delivery_center(
        self,
        *,
        webhook_limit: int = 50,
        webhook_before: tuple[datetime, int] | None = None,
        notification_limit: int = 50,
        notification_before: tuple[datetime, int] | None = None,
    ) -> DeliveryCenter:
        """Return one consistent, read-only view; no dispatcher cursor is touched."""

        _validate_limit(webhook_limit)
        _validate_limit(notification_limit)
        async with self._tx(own=True) as tx:
            now = await tx.now()
            channels = await tx.notification_channels()
            rules = await tx.notification_rules()
            endpoints = await tx.webhook_endpoint_reads()
            webhook_rows = await tx.webhook_delivery_read_page(limit=webhook_limit + 1, before=webhook_before)
            notification_rows = await tx.notification_delivery_read_page(
                limit=notification_limit + 1, before=notification_before
            )
            preview_started_at = now - timedelta(hours=24)
            events = await tx.event_read_page(
                EventReadFilter(
                    allowed_types=frozenset(
                        event_type for event_type, spec in CATALOG.items() if spec.external
                    ),
                    occurred_from=preview_started_at,
                    occurred_to=now,
                ),
                limit=100_000,
                before=None,
            )

        identity_ids = [row.identity_id for row in events if row.identity_id is not None]
        labels = await self.hub.store.identity_labels(identity_ids)
        preview_counts = {
            rule.id: sum(
                _matches_rule(
                    rule,
                    event,
                    labels.get(event.identity_id, {}) if event.identity_id is not None else {},
                )
                for event in events
            )
            for rule in rules
        }
        visible_webhooks = webhook_rows[:webhook_limit]
        visible_notifications = notification_rows[:notification_limit]
        return DeliveryCenter(
            channels=tuple(
                NotificationChannelItem(
                    id=row.id,
                    title=row.title,
                    kind="telegram",
                    masked_target=_mask_chat_id(row.config.get("chat_id", "unknown")),
                    thread_id=row.config.get("thread_id"),
                    enabled=row.enabled,
                    created_at=row.created_at,
                )
                for row in channels
            ),
            rules=tuple(
                NotificationRuleItem(
                    id=row.id,
                    channel_id=row.channel_id,
                    name=row.name,
                    enabled=row.enabled,
                    event_types=tuple(row.event_types),
                    min_severity=row.min_severity,
                    filter=scrub(row.filter),
                    digest_seconds=row.digest_seconds,
                    template_configured=row.template is not None,
                    preview_24h=preview_counts[row.id],
                    updated_at=row.updated_at,
                )
                for row in rules
            ),
            webhook_endpoints=tuple(
                WebhookEndpointItem(
                    id=row.id,
                    masked_target=_mask_webhook_url(row.url),
                    description=row.description,
                    event_types=tuple(row.event_types),
                    filter=scrub(row.filter),
                    min_severity=row.min_severity,
                    enabled=row.enabled,
                    disabled_reason=_disabled_reason(row.disabled_reason),
                    failure_streak=row.failure_streak,
                    created_at=row.created_at,
                )
                for row in endpoints
            ),
            webhook_deliveries=WebhookDeliveryPage(
                items=tuple(
                    WebhookDeliveryItem(
                        id=row.id,
                        endpoint_id=row.endpoint_id,
                        event_id=row.event_id,
                        status=row.status,
                        attempt=row.attempt,
                        next_attempt_at=row.next_attempt_at,
                        response_code=row.response_code,
                        response_ms=row.response_ms,
                        has_error=row.error is not None,
                        error_summary="delivery_failed" if row.error is not None else None,
                        created_at=row.created_at,
                        delivered_at=row.delivered_at,
                    )
                    for row in visible_webhooks
                ),
                next_before=(visible_webhooks[-1].created_at, visible_webhooks[-1].id)
                if len(webhook_rows) > webhook_limit and visible_webhooks
                else None,
            ),
            notifications=NotificationDeliveryPage(
                items=tuple(
                    NotificationDeliveryItem(
                        id=row.id,
                        rule_id=row.rule_id,
                        rule_name=row.rule_name,
                        channel_id=row.channel_id,
                        channel_title=row.channel_title,
                        event_ids=tuple(row.event_ids),
                        event_count=len(row.event_ids),
                        status=row.status,
                        attempt=row.attempt,
                        scheduled_at=row.scheduled_at,
                        sent_at=row.sent_at,
                        has_error=row.error is not None,
                        error_summary="delivery_failed" if row.error is not None else None,
                    )
                    for row in visible_notifications
                ),
                next_before=(visible_notifications[-1].scheduled_at, visible_notifications[-1].id)
                if len(notification_rows) > notification_limit and visible_notifications
                else None,
            ),
            preview_window_started_at=preview_started_at,
        )
