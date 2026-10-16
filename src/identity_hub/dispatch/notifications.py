"""Telegram-уведомления: правила, дайджесты, шаблоны и очередь (ТЗ FR-15)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from string import Formatter
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from pydantic import BaseModel, SecretStr

from identity_hub._ids import new_id
from identity_hub.api._base import Namespace
from identity_hub.crypto import associated_data
from identity_hub.domain.data import validate_json_payload
from identity_hub.enums import Severity
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import Event, matches_any, severity_at_least, spec_of
from identity_hub.identities import UNSET, _Unset
from identity_hub.store.base import (
    NewNotificationChannel,
    NewNotificationRule,
    NotificationChannelRow,
    NotificationEnqueue,
    NotificationRuleRow,
    NotificationTargetRow,
)

if TYPE_CHECKING:
    from identity_hub.hub import IdentityHub, SpaceHandle

__all__ = [
    "NotificationChannel",
    "NotificationDispatcher",
    "NotificationRule",
    "Notifications",
    "TelegramRateLimited",
    "TelegramRejected",
    "TelegramTransport",
    "TelegramUnavailable",
]


TELEGRAM_MESSAGE_LIMIT = 4096
"""Предел длины сообщения Bot API; считается в единицах UTF-16."""


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


class TelegramRejected(Exception):
    """Bot API отклонил запрос (неверный токен, чат не найден, бот заблокирован): повтор бесполезен."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(detail[:300])


class TelegramUnavailable(Exception):
    """Временная ошибка сети или Bot API (5xx): доставка повторяется по политике ретраев."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail[:300])


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


def _validate_filter(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(value or {})
    for key, expected in result.items():
        if key not in {"service", "labels"} and not key.startswith("data."):
            raise ValidationFailed("фильтр уведомления содержит неизвестные поля")
        if key == "labels" and not isinstance(expected, Mapping):
            raise ValidationFailed("labels в фильтре уведомления должен быть объектом")
    return result


def _validate_template(template: str | None) -> str | None:
    if template is None:
        return None
    if not template.strip() or len(template) > 4096:
        raise ValidationFailed("шаблон уведомления должен содержать от 1 до 4096 символов")
    allowed = {"type", "severity", "service", "actor", "link", "identity_id", "occurred_at"}
    try:
        parsed = [(name, spec or "") for _, name, spec, _ in Formatter().parse(template) if name is not None]
    except ValueError as exc:
        raise ValidationFailed("ошибка шаблона уведомления") from exc
    if any(name not in allowed for name, _ in parsed):
        raise ValidationFailed("шаблон уведомления содержит неизвестное или небезопасное поле")
    # format-spec подставляется отдельно: `{type:{data}}` вывел бы поле в обход проверки имён
    if any("{" in spec or "}" in spec for _, spec in parsed):
        raise ValidationFailed("шаблон уведомления: вложенные поля в формате не поддерживаются")
    return template


class Notifications(Namespace):
    """CRUD каналов и правил одного пространства."""

    __slots__ = ()

    @property
    def space(self) -> SpaceHandle:
        return self._space

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
        secret = self.space.hub._seal_stored_model(
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
        thread_id: int | _Unset | None = UNSET,
        enabled: bool | None = None,
    ) -> None:
        """Изменить канал. `thread_id=None` сбрасывает тему; пропущенный аргумент оставляет её."""
        values: dict[str, Any] = {}
        if title is not None:
            values["title"] = title
        if enabled is not None:
            values["enabled"] = enabled
        channels = await self.channels()
        current = next((item for item in channels if item.id == channel_id), None)
        if current is None:
            raise NotFound("notification channel", channel_id)
        if chat_id is not None or not isinstance(thread_id, _Unset):
            values["config"] = {
                "chat_id": current.chat_id if chat_id is None else chat_id,
                "thread_id": current.thread_id if isinstance(thread_id, _Unset) else thread_id,
            }
        secret = None
        if bot_token is not None:
            secret = self.space.hub._seal_stored_model(
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
        validate_json_payload(dict(filter or {}), "filter")
        async with self.space._tx() as tx:
            row = await tx.insert_notification_rule(
                NewNotificationRule(
                    id or new_id(),
                    channel_id,
                    name,
                    _validate_patterns(event_types),
                    str(min_severity),
                    _validate_filter(filter),
                    digest_seconds,
                    _validate_template(template),
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
        if values.get("filter") is not None:
            validate_json_payload(dict(values["filter"]), "filter")
        if "event_types" in values:
            values["event_types"] = _validate_patterns(values["event_types"])
        if "filter" in values:
            values["filter"] = _validate_filter(values["filter"])
        if "template" in values:
            values["template"] = _validate_template(values["template"])
        if (
            "digest_seconds" in values
            and values["digest_seconds"] is not None
            and not 10 <= values["digest_seconds"] <= 86400
        ):
            raise ValidationFailed("digest_seconds должен быть от 10 до 86400")
        if isinstance(values.get("min_severity"), Severity):
            values["min_severity"] = str(values["min_severity"])
        async with self.space._tx() as tx:
            if not await tx.update_notification_rule(rule_id, values):
                raise NotFound("notification rule", rule_id)

    async def delete_rule(self, rule_id: UUID) -> None:
        async with self.space._tx() as tx:
            if not await tx.delete_notification_rule(rule_id):
                raise NotFound("notification rule", rule_id)

    async def send_test(self, channel_id: UUID, text: str) -> None:
        """Send a direct probe without creating a notification delivery or touching cursors."""
        if not text.strip() or len(text) > 4096:
            raise ValidationFailed("тестовое сообщение должно содержать от 1 до 4096 символов")
        async with self.space._tx() as tx:
            row = await tx.get_notification_channel_secret(channel_id)
        if row is None:
            raise NotFound("notification channel", channel_id)
        token = self.space.hub._open_stored_model(
            _TelegramSecret,
            row.secret,
            associated_data("ih_notification_channels", channel_id),
        ).bot_token
        await self.space.hub._notification_dispatcher.transport.send_message(
            token,
            row.config["chat_id"],
            text,
            thread_id=row.config.get("thread_id"),
        )

    async def retry(self, delivery_id: int) -> None:
        async with self.space._tx() as tx:
            if not await tx.retry_notification_delivery(delivery_id):
                raise NotFound("notification delivery", delivery_id)


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
        # уведомление → сколько частей дайджеста уже ушло: повтор после 429 не шлёт их снова
        self._sent_parts: dict[int, int] = {}

    async def handle_events(self, events: list[Event]) -> None:
        targets = await self.hub._store.enabled_notification_targets()
        identity_ids = [event.identity_id for event in events if event.identity_id is not None]
        labels = await self.hub._store.identity_labels(identity_ids)
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
        await self.hub._store.enqueue_notifications(items)

    async def deliver_due(self) -> int:
        attempts = await self.hub._store.claim_notifications(
            limit=self.policy.batch_size, claim_for=self.policy.claim_for
        )
        paused: dict[UUID, timedelta] = {}  # каналы, ответившие 429 в этой пачке
        for attempt in attempts:
            retry_after = self.policy.retry_after(attempt.attempt)
            succeeded = False
            error: str | None = None
            max_attempts = self.policy.max_failures
            pause = paused.get(attempt.channel_id)
            if pause is not None:
                error = "TelegramRateLimited"  # канал на паузе: запрос не отправляем
            else:
                try:
                    token = self.hub._open_stored_model(
                        _TelegramSecret,
                        attempt.channel_secret,
                        associated_data("ih_notification_channels", attempt.channel_id),
                    ).bot_token
                    config = attempt.channel_config
                    messages = _render_messages(attempt.events, attempt.template, self.hub.admin_base_url)
                    sent = self._sent_parts.get(attempt.notification_id, 0)
                    for index in range(sent, len(messages)):
                        await self.transport.send_message(
                            token, config["chat_id"], messages[index], thread_id=config.get("thread_id")
                        )
                        self._sent_parts[attempt.notification_id] = index + 1
                    succeeded = True
                except TelegramRateLimited as exc:
                    # 429 — не ошибка доставки: попытка не расходуется, канал ждёт retry_after
                    pause = paused[attempt.channel_id] = timedelta(seconds=max(1.0, exc.retry_after))
                    error = "TelegramRateLimited"
                except TelegramRejected as exc:
                    # постоянная ошибка: сразу в dead, без исчерпания ретраев
                    max_attempts = attempt.attempt
                    error = f"TelegramRejected: {exc}"
                except TelegramUnavailable as exc:
                    error = f"TelegramUnavailable: {exc}"
                except Exception as exc:
                    error = type(exc).__name__
            if succeeded or (pause is None and attempt.attempt >= max_attempts):
                self._sent_parts.pop(attempt.notification_id, None)
            await self.hub._store.finish_notification(
                attempt.notification_id,
                attempt.attempt,
                succeeded=succeeded,
                error=error,
                retry_after=pause or retry_after,
                max_attempts=max_attempts,
                pause_channel=pause,
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


def _units(text: str) -> int:
    """Длина в единицах UTF-16 — так лимит считает Bot API."""
    return len(text.encode("utf-16-le")) // 2


def _fit(text: str) -> str:
    """Обрезать сообщение до лимита Bot API, пометив обрезку многоточием."""
    if _units(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    kept: list[str] = []
    used = 1  # место под «…»
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > TELEGRAM_MESSAGE_LIMIT:
            break
        kept.append(char)
        used += size
    return "".join(kept) + "…"


def _render_messages(
    events: tuple[Event, ...], template: str | None, admin_base_url: str | None
) -> list[str]:
    """Тексты для отправки: одно событие — одно сообщение, дайджест — частями до лимита Bot API."""
    lines = [_fit(_render_event(event, template, admin_base_url)) for event in events]
    if len(lines) <= 1:
        return lines
    messages: list[str] = []
    header = f"Сводка Identity Hub ({len(lines)}):"
    current = header
    for line in lines:
        candidate = f"{current}\n\n{line}"
        if _units(candidate) <= TELEGRAM_MESSAGE_LIMIT:
            current = candidate
        elif current == header:  # событие почти во весь лимит: заголовок не шлём отдельным сообщением
            current = _fit(candidate)
        else:
            messages.append(current)
            current = line
    messages.append(current)
    return messages


def _render(events: tuple[Event, ...], template: str | None, admin_base_url: str | None) -> str:
    """Текст первого сообщения (совместимость для вызывающих, которым нужен один текст)."""
    messages = _render_messages(events, template, admin_base_url)
    return messages[0] if messages else ""


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
