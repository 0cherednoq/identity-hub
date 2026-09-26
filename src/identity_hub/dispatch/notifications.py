"""Telegram-уведомления: правила, дайджесты, шаблоны и очередь (ТЗ FR-15)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import BaseModel, SecretStr

from identity_hub._ids import new_id
from identity_hub.crypto import associated_data
from identity_hub.enums import Severity
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import Event, matches_any, severity_at_least, spec_of
from identity_hub.store.base import (
    NewNotificationChannel,
    NewNotificationRule,
    NotificationChannelRow,
    NotificationEnqueue,
    NotificationRuleRow,
    NotificationTargetRow,
)

if TYPE_CHECKING:
    from identity_hub._space import SpaceBase
    from identity_hub.hub import IdentityHub

__all__ = [
    "NotificationChannel",
    "NotificationDispatcher",
    "NotificationRule",
    "Notifications",
    "TelegramRateLimited",
    "TelegramTransport",
]


class TelegramTransport(Protocol):
    async def send_message(
        self,
        bot_token: SecretStr,
        chat_id: int | str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None: ...


class TelegramRateLimited(Exception):
    def __init__(self, retry_after: int | float) -> None:
        self.retry_after = float(retry_after)
        super().__init__("Telegram rate limit")


class _TelegramSecret(BaseModel):
    bot_token: SecretStr


@dataclass(frozen=True, slots=True)
class NotificationChannel:
    id: UUID
    title: str
    chat_id: int | str
    thread_id: int | None
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationRule:
    id: UUID
    channel_id: UUID
    name: str
    enabled: bool
    event_types: tuple[str, ...]
    min_severity: Severity
    filter: Mapping[str, Any]
    digest_seconds: int | None
    template: str | None
    created_by: str
    updated_at: datetime


def _channel(row: NotificationChannelRow) -> NotificationChannel:
    return NotificationChannel(
        id=row.id,
        title=row.title,
        chat_id=row.config["chat_id"],
        thread_id=row.config.get("thread_id"),
        enabled=row.enabled,
        created_at=row.created_at,
    )


def _rule(row: NotificationRuleRow) -> NotificationRule:
    return NotificationRule(
        id=row.id,
        channel_id=row.channel_id,
        name=row.name,
        enabled=row.enabled,
        event_types=tuple(row.event_types),
        min_severity=Severity(row.min_severity),
        filter=dict(row.filter),
        digest_seconds=row.digest_seconds,
        template=row.template,
        created_by=row.created_by,
        updated_at=row.updated_at,
    )


def _validate_patterns(patterns: Iterable[str]) -> list[str]:
    result = list(dict.fromkeys(patterns))
    if not result:
        raise ValidationFailed("правило должно содержать хотя бы один тип события")
    for pattern in result:
        if pattern != "*" and not pattern.endswith(".*"):
            try:
                spec_of(pattern)
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
    return result


class Notifications:
    """CRUD каналов и правил одного пространства."""

    def __init__(self, space: SpaceBase) -> None:
        self.space = space

    async def create_telegram_channel(
        self,
        title: str,
        *,
        bot_token: SecretStr,
        chat_id: int | str,
        thread_id: int | None = None,
        id: UUID | None = None,
    ) -> NotificationChannel:
        if not bot_token.get_secret_value():
            raise ValidationFailed("bot_token не может быть пустым")
        channel_id = id or new_id()
        secret = self.space.hub.envelope.seal_model(
            _TelegramSecret(bot_token=bot_token),
            associated_data("ih_notification_channels", channel_id),
        )
        async with self.space._tx() as tx:
            row = await tx.insert_notification_channel(
                NewNotificationChannel(
                    channel_id,
                    title,
                    {"chat_id": chat_id, "thread_id": thread_id},
                    secret,
                )
            )
        return _channel(row)

    async def channels(self) -> list[NotificationChannel]:
        async with self.space._tx() as tx:
            return [_channel(row) for row in await tx.notification_channels()]

    async def update_telegram_channel(
        self,
        channel_id: UUID,
        *,
        title: str | None = None,
        bot_token: SecretStr | None = None,
        chat_id: int | str | None = None,
        thread_id: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        values: dict[str, Any] = {}
        if title is not None:
            values["title"] = title
        if enabled is not None:
            values["enabled"] = enabled
        channels = await self.channels()
        current = next((item for item in channels if item.id == channel_id), None)
        if current is None:
            raise NotFound("notification channel", channel_id)
        if chat_id is not None or thread_id is not None:
            values["config"] = {
                "chat_id": current.chat_id if chat_id is None else chat_id,
                "thread_id": current.thread_id if thread_id is None else thread_id,
            }
        secret = None
        if bot_token is not None:
            secret = self.space.hub.envelope.seal_model(
                _TelegramSecret(bot_token=bot_token),
                associated_data("ih_notification_channels", channel_id),
            )
        async with self.space._tx() as tx:
            if not await tx.update_notification_channel(channel_id, values, secret):
                raise NotFound("notification channel", channel_id)

    async def delete_channel(self, channel_id: UUID) -> None:
        async with self.space._tx() as tx:
            if not await tx.delete_notification_channel(channel_id):
                raise NotFound("notification channel", channel_id)

    async def create_rule(
        self,
        *,
        name: str,
        channel_id: UUID,
        event_types: Iterable[str],
        filter: Mapping[str, Any] | None = None,
        min_severity: Severity = Severity.WARNING,
        digest_seconds: int | None = None,
        template: str | None = None,
        created_by: str,
        id: UUID | None = None,
    ) -> NotificationRule:
        if digest_seconds is not None and not 10 <= digest_seconds <= 86400:
            raise ValidationFailed("digest_seconds должен быть от 10 до 86400")
        async with self.space._tx() as tx:
            row = await tx.insert_notification_rule(
                NewNotificationRule(
                    id or new_id(),
                    channel_id,
                    name,
                    _validate_patterns(event_types),
                    str(min_severity),
                    dict(filter or {}),
                    digest_seconds,
                    template,
                    created_by,
                )
            )
        return _rule(row)

    async def rules(self) -> list[NotificationRule]:
        async with self.space._tx() as tx:
            return [_rule(row) for row in await tx.notification_rules()]

    async def update_rule(self, rule_id: UUID, **values: Any) -> None:
        allowed = {
            "name",
            "enabled",
            "event_types",
            "min_severity",
            "filter",
            "digest_seconds",
            "template",
        }
        if not values.keys() <= allowed:
            raise ValidationFailed("неизвестные поля правила уведомлений")
        if "event_types" in values:
            values["event_types"] = _validate_patterns(values["event_types"])
        if isinstance(values.get("min_severity"), Severity):
            values["min_severity"] = str(values["min_severity"])
        async with self.space._tx() as tx:
            if not await tx.update_notification_rule(rule_id, values):
                raise NotFound("notification rule", rule_id)

    async def delete_rule(self, rule_id: UUID) -> None:
        async with self.space._tx() as tx:
            if not await tx.delete_notification_rule(rule_id):
                raise NotFound("notification rule", rule_id)


class NotificationDispatcher:
    def __init__(
        self,
        hub: IdentityHub,
        transport: TelegramTransport,
        *,
        policy: Any,
    ) -> None:
        self.hub = hub
        self.transport = transport
        self.policy = policy

    async def handle_events(self, events: list[Event]) -> None:
        targets = await self.hub.store.enabled_notification_targets()
        identity_ids = [event.identity_id for event in events if event.identity_id is not None]
        labels = await self.hub.store.identity_labels(identity_ids)
        items: list[NotificationEnqueue] = []
        for event in events:
            event_labels = labels.get(event.identity_id, {}) if event.identity_id is not None else {}
            for target in targets:
                if not _matches_target(target, event, event_labels):
                    continue
                digest = target.rule.digest_seconds
                if digest is None:
                    key = None
                    scheduled = event.occurred_at
                else:
                    epoch = int(event.occurred_at.timestamp())
                    end = (epoch // digest + 1) * digest
                    key = f"{target.rule.id}:{end}"
                    scheduled = datetime.fromtimestamp(end, UTC)
                items.append(
                    NotificationEnqueue(
                        target.rule.id,
                        target.rule.channel_id,
                        event.event_id,
                        key,
                        scheduled,
                    )
                )
        await self.hub.store.enqueue_notifications(items)

    async def deliver_due(self) -> int:
        attempts = await self.hub.store.claim_notifications(
            limit=self.policy.batch_size, claim_for=self.policy.claim_for
        )
        for attempt in attempts:
            retry_after = self.policy.retry_after(attempt.attempt)
            succeeded = False
            error: str | None = None
            try:
                token = self.hub.envelope.open_model(
                    _TelegramSecret,
                    attempt.channel_secret,
                    associated_data("ih_notification_channels", attempt.channel_id),
                ).bot_token
                config = attempt.channel_config
                await self.transport.send_message(
                    token,
                    config["chat_id"],
                    _render(attempt.events, attempt.template, self.hub.admin_base_url),
                    thread_id=config.get("thread_id"),
                )
                succeeded = True
            except TelegramRateLimited as exc:
                retry_after = timedelta(seconds=exc.retry_after)
                error = "TelegramRateLimited"
            except Exception as exc:
                error = type(exc).__name__
            await self.hub.store.finish_notification(
                attempt.notification_id,
                attempt.attempt,
                succeeded=succeeded,
                error=error,
                retry_after=retry_after,
                max_attempts=self.policy.max_failures,
            )
        return len(attempts)


def _matches_target(target: NotificationTargetRow, event: Event, labels: Mapping[str, Any]) -> bool:
    rule = target.rule
    if target.space_slug != event.space or not matches_any(rule.event_types, event.type):
        return False
    if not severity_at_least(str(event.severity), rule.min_severity):
        return False
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
            actual = event.data.get(key[5:])
        else:
            return False
        if actual not in allowed:
            return False
    return True


def _render(events: tuple[Event, ...], template: str | None, admin_base_url: str | None) -> str:
    lines = [_render_event(event, template, admin_base_url) for event in events]
    return lines[0] if len(lines) == 1 else f"Сводка Identity Hub ({len(lines)}):\n\n" + "\n\n".join(lines)


def _render_event(event: Event, template: str | None, admin_base_url: str | None) -> str:
    link = (
        f"{admin_base_url}/identities/{event.identity_id}"
        if admin_base_url and event.identity_id is not None
        else ""
    )
    values = {
        **asdict(event),
        "severity": str(event.severity),
        "data": dict(event.data),
        "link": link,
    }
    if template:
        try:
            return template.format_map(values)
        except (KeyError, ValueError) as exc:
            raise ValidationFailed("ошибка шаблона уведомления") from exc
    transition = ""
    if "from" in event.data or "to" in event.data:
        transition = f"\n{event.data.get('from', '—')} → {event.data.get('to', '—')}"
    suffix = f"\n{link}" if link else ""
    return f"[{str(event.severity).upper()}] {event.type}{transition}{suffix}"
