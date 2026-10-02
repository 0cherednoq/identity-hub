"""Исходящие вебхуки: endpoints, HMAC и доставка (ТЗ FR-14, S-9)."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, SecretStr

from identity_hub._ids import new_id
from identity_hub.crypto import associated_data
from identity_hub.dispatch.outbound import (
    Resolver,
    create_client,
    host_address,
    is_public_address,
    mask_url,
    masked_in_logs,
    system_resolver,
    url_log_masks,
)
from identity_hub.domain.data import validate_json_payload
from identity_hub.enums import Severity
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import Event, matches_any, severity_at_least, spec_of
from identity_hub.store.base import NewWebhookEndpoint, WebhookDeliveryRow, WebhookEndpointRow

if TYPE_CHECKING:
    import httpx

    from identity_hub._space import SpaceBase
    from identity_hub.hub import IdentityHub

__all__ = [
    "WebhookDelivery",
    "WebhookDispatcher",
    "WebhookEndpoint",
    "WebhookPolicy",
    "Webhooks",
    "make_signature",
    "validate_webhook_url",
    "verify_signature",
]

SIGNATURE_HEADER = "X-Identity-Hub-Signature"
REPLAY_WINDOW = timedelta(minutes=5)


class _WebhookSecrets(BaseModel):
    current: SecretStr
    previous: SecretStr | None = None
    previous_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class WebhookPolicy:
    timeout: float = 10.0
    max_failures: int = 10
    retry_base: timedelta = timedelta(seconds=30)
    retry_cap: timedelta = timedelta(hours=24)
    retry_jitter: float = 0.2
    claim_for: timedelta = timedelta(seconds=30)
    batch_size: int = 100
    # Исходящий прокси задаётся только явно: HTTP_PROXY/ALL_PROXY окружения не используются.
    # С прокси имя получателя резолвит он, поэтому закрепление проверенного адреса не действует.
    proxy: str | None = None

    def retry_after(self, attempt: int) -> timedelta:
        base = min(self.retry_cap.total_seconds(), self.retry_base.total_seconds() * 2 ** (attempt - 1))
        jitter = random.uniform(0, base * self.retry_jitter) if base > 0 else 0
        return timedelta(seconds=min(self.retry_cap.total_seconds(), base + jitter))


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    id: UUID
    url: str  # `scheme://host/***`: путь и query могут содержать секрет получателя (R2-S15)
    description: str | None
    event_types: tuple[str, ...]
    filter: Mapping[str, Any]
    min_severity: Severity
    enabled: bool
    disabled_reason: str | None
    failure_streak: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    id: int
    endpoint_id: UUID
    event_id: UUID
    status: str
    attempt: int
    next_attempt_at: datetime
    response_code: int | None
    response_ms: int | None
    error: str | None
    created_at: datetime
    delivered_at: datetime | None


def _secret_bytes(secret: SecretStr | str | bytes) -> bytes:
    value: str | bytes = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
    return value if isinstance(value, bytes) else value.encode()


def make_signature(
    body: bytes,
    secrets: SecretStr | str | bytes | Sequence[SecretStr | str | bytes],
    *,
    timestamp: int | None = None,
) -> str:
    """Сформировать подпись; при ротации заголовок содержит несколько полей `v1` (FR-14.2)."""
    values = [secrets] if isinstance(secrets, SecretStr | str | bytes) else list(secrets)
    if not values:
        raise ValidationFailed("для подписи нужен хотя бы один секрет")
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = str(timestamp).encode() + b"." + body
    signatures = [hmac.new(_secret_bytes(secret), signed, hashlib.sha256).hexdigest() for secret in values]
    return ",".join([f"t={timestamp}", *(f"v1={value}" for value in signatures)])


def verify_signature(
    body: bytes,
    header: str,
    secrets: SecretStr | str | bytes | Sequence[SecretStr | str | bytes],
    *,
    now: datetime | int | float | None = None,
    tolerance: timedelta = REPLAY_WINDOW,
) -> bool:
    """Проверить HMAC и окно повтора 5 минут; подходит получателям вебхука (S-9).

    `now` — текущее время получателя (`datetime` или Unix-время); по умолчанию системное.
    В тестах с `FakeClock` передавайте `now=clock.now()`: подпись несёт время часов хаба.
    """
    try:
        parts = [part.split("=", 1) for part in header.split(",")]
        timestamp = int(next(value for key, value in parts if key == "t"))
        candidates = [value for key, value in parts if key == "v1"]
    except (StopIteration, ValueError):
        return False
    current = now.timestamp() if isinstance(now, datetime) else float(now if now is not None else time.time())
    if abs(current - timestamp) > tolerance.total_seconds():
        return False
    values = [secrets] if isinstance(secrets, SecretStr | str | bytes) else list(secrets)
    signed = str(timestamp).encode() + b"." + body
    return any(
        hmac.compare_digest(hmac.new(_secret_bytes(secret), signed, hashlib.sha256).hexdigest(), candidate)
        for secret in values
        for candidate in candidates
    )


def validate_webhook_url(url: str, *, dev_hosts: Iterable[str] = ()) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise ValidationFailed("URL вебхука должен содержать host")
    if parsed.username is not None or parsed.password is not None:
        raise ValidationFailed("URL вебхука не должен содержать credentials")
    dev = {item.casefold().rstrip(".") for item in dev_hosts}
    normalized_host = host.casefold().rstrip(".")
    if normalized_host not in dev:
        # литерал в любой форме, которую резолвер читает как IPv4: 2130706433, 0x7f000001, 127.1, 0177.0.0.1
        address = host_address(normalized_host)
        if address is not None:
            if not is_public_address(address):
                raise ValidationFailed("URL вебхука не может указывать на приватный или служебный адрес")
        elif "." not in normalized_host or normalized_host.endswith((".localhost", ".local", ".internal")):
            # имя без точки ищется в search-домене сети приложения (`metadata`, `redis`)
            raise ValidationFailed("URL вебхука не может указывать на локальный адрес")
        # Остальные имена проверяются при соединении: адрес резолвится и закрепляется (PinnedBackend).
    if parsed.scheme == "https":
        return url
    if parsed.scheme == "http" and normalized_host in dev:
        return url
    raise ValidationFailed("URL вебхука должен использовать https (http разрешён только dev-hostам)")


def _validated_patterns(event_types: Iterable[str]) -> list[str]:
    patterns = list(dict.fromkeys(event_types))
    if not patterns:
        raise ValidationFailed("endpoint должен содержать хотя бы один тип события")
    for pattern in patterns:
        if pattern != "*" and not pattern.endswith(".*"):
            try:
                spec_of(pattern)
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
        if pattern.endswith(".*") and len(pattern) <= 2:
            raise ValidationFailed(f"некорректная маска события: {pattern!r}")
    return patterns


def _validated_filter(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(value or {})
    unknown = set(result) - {"service", "labels"}
    if unknown:
        raise ValidationFailed("фильтр webhook содержит неизвестные поля")
    labels = result.get("labels")
    if labels is not None and not isinstance(labels, Mapping):
        raise ValidationFailed("labels в фильтре webhook должен быть объектом")
    return result


def _endpoint(row: WebhookEndpointRow) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=row.id,
        url=mask_url(row.url),
        description=row.description,
        event_types=tuple(row.event_types),
        filter=dict(row.filter),
        min_severity=Severity(row.min_severity),
        enabled=row.enabled,
        disabled_reason=row.disabled_reason,
        failure_streak=row.failure_streak,
        created_at=row.created_at,
    )


def _delivery(row: WebhookDeliveryRow) -> WebhookDelivery:
    return WebhookDelivery(**{name: getattr(row, name) for name in WebhookDelivery.__dataclass_fields__})


class Webhooks:
    """Управление endpoint'ами одного пространства (слой `SpaceHandle.Webhooks`)."""

    def __init__(self, space: SpaceBase) -> None:
        self.space = space

    async def create(
        self,
        *,
        url: str,
        secret: SecretStr,
        event_types: Iterable[str],
        filter: Mapping[str, Any] | None = None,
        min_severity: Severity = Severity.INFO,
        description: str | None = None,
        id: UUID | None = None,
    ) -> WebhookEndpoint:
        url = validate_webhook_url(url, dev_hosts=self.space.hub.webhook_dev_hosts)
        patterns = _validated_patterns(event_types)
        validate_json_payload(dict(filter or {}), "filter")
        if not secret.get_secret_value():
            raise ValidationFailed("секрет вебхука не может быть пустым")
        endpoint_id = id or new_id()
        payload = self.space.hub._envelope.seal_model(
            _WebhookSecrets(current=secret), associated_data("ih_webhook_endpoints", endpoint_id)
        )
        async with self.space._tx() as tx:
            row = await tx.insert_webhook_endpoint(
                NewWebhookEndpoint(
                    endpoint_id,
                    url,
                    description,
                    patterns,
                    _validated_filter(filter),
                    str(min_severity),
                    payload,
                )
            )
        return _endpoint(row)

    async def update(
        self,
        endpoint_id: UUID,
        *,
        url: str,
        event_types: Iterable[str],
        filter: Mapping[str, Any] | None = None,
        min_severity: Severity = Severity.INFO,
        description: str | None = None,
    ) -> None:
        url = validate_webhook_url(url, dev_hosts=self.space.hub.webhook_dev_hosts)
        validate_json_payload(dict(filter or {}), "filter")
        async with self.space._tx() as tx:
            changed = await tx.update_webhook_endpoint(
                endpoint_id,
                url=url,
                description=description,
                event_types=_validated_patterns(event_types),
                filter=_validated_filter(filter),
                min_severity=str(min_severity),
            )
        if not changed:
            raise NotFound("webhook endpoint", endpoint_id)

    async def delete(self, endpoint_id: UUID) -> None:
        async with self.space._tx() as tx:
            if not await tx.delete_webhook_endpoint(endpoint_id):
                raise NotFound("webhook endpoint", endpoint_id)

    async def rotate_secret(
        self, endpoint_id: UUID, secret: SecretStr, *, overlap: timedelta = timedelta(hours=1)
    ) -> None:
        if overlap < timedelta(0):
            raise ValidationFailed("период перекрытия секретов не может быть отрицательным")
        async with self.space._tx() as tx:
            row = await tx.get_webhook_endpoint(endpoint_id)
            if row is None:
                raise NotFound("webhook endpoint", endpoint_id)
            current = self.space.hub._envelope.open_model(
                _WebhookSecrets,
                row.secret,
                associated_data("ih_webhook_endpoints", endpoint_id),
            )
            rotated = _WebhookSecrets(
                current=secret,
                previous=current.current,
                previous_until=self.space.hub.clock.now() + overlap,
            )
            payload = self.space.hub._envelope.seal_model(
                rotated, associated_data("ih_webhook_endpoints", endpoint_id)
            )
            if not await tx.update_webhook_secret(endpoint_id, payload):
                raise NotFound("webhook endpoint", endpoint_id)

    async def set_enabled(self, endpoint_id: UUID, enabled: bool, *, reason: str | None = None) -> None:
        async with self.space._tx() as tx:
            if not await tx.set_webhook_enabled(endpoint_id, enabled, reason):
                raise NotFound("webhook endpoint", endpoint_id)

    async def retry(self, delivery_id: int) -> None:
        async with self.space._tx() as tx:
            if not await tx.retry_webhook_delivery(delivery_id):
                raise NotFound("webhook delivery", delivery_id)

    async def send_test(self, endpoint_id: UUID) -> int:
        """Send a signed probe without enqueueing a delivery or advancing any event cursor."""
        async with self.space._tx() as tx:
            row = await tx.get_webhook_endpoint(endpoint_id)
        if row is None:
            raise NotFound("webhook endpoint", endpoint_id)
        validate_webhook_url(row.url, dev_hosts=self.space.hub.webhook_dev_hosts)
        secrets = self.space.hub._envelope.open_model(
            _WebhookSecrets,
            row.secret,
            associated_data("ih_webhook_endpoints", endpoint_id),
        )
        body = json.dumps(
            {
                "type": "identity_hub.webhook.test",
                "endpoint_id": str(endpoint_id),
                "sent_at": self.space.hub.clock.now().isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        dispatcher = self.space.hub._webhook_dispatcher
        failure: str | None = None
        response = None
        try:
            async with dispatcher.client() as client:
                with masked_in_logs(*url_log_masks(row.url)):
                    response = await client.post(
                        row.url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            SIGNATURE_HEADER: make_signature(body, secrets.current),
                        },
                    )
        except Exception as exc:
            failure = type(exc).__name__
        if response is None:
            # вне except: исключение httpx несёт полный URL и не должно остаться в цепочке
            raise ValidationFailed(f"тестовая доставка не выполнена: {failure or 'unknown'}")
        return response.status_code

    async def deliveries(self, endpoint_id: UUID, *, limit: int = 100) -> list[WebhookDelivery]:
        async with self.space._tx() as tx:
            rows = await tx.webhook_deliveries(endpoint_id, limit)
        return [_delivery(row) for row in rows]


class WebhookDispatcher:
    def __init__(self, hub: IdentityHub, policy: WebhookPolicy | None = None) -> None:
        self.hub = hub
        self.policy = policy or WebhookPolicy()
        self.resolver: Resolver = system_resolver  # подменяется в тестах

    def client(self) -> httpx.AsyncClient:
        """Клиент доставки: без переменных окружения и редиректов, с закреплением проверенного адреса."""
        try:
            import httpx  # noqa: F401
        except ImportError as exc:  # pragma: no cover - окружение без extra webhooks
            raise RuntimeError("для вебхуков установите identity-hub[webhooks]") from exc
        return create_client(
            timeout=self.policy.timeout,
            proxy=self.policy.proxy,
            pin=True,
            resolver=self.resolver,
            allow_hosts=self.hub.webhook_dev_hosts,
        )

    async def handle_events(self, events: list[Event]) -> None:
        endpoints = await self.hub._store.enabled_webhook_endpoints()
        identity_ids = [event.identity_id for event in events if event.identity_id is not None]
        labels = await self.hub._store.identity_labels(identity_ids)
        pairs = [
            (endpoint.id, event.event_id)
            for event in events
            for endpoint in endpoints
            if _matches_endpoint(
                endpoint,
                event,
                labels.get(event.identity_id, {}) if event.identity_id is not None else {},
            )
        ]
        await self.hub._store.enqueue_webhook_deliveries(pairs)

    async def deliver_due(self) -> int:
        attempts = await self.hub._store.claim_webhook_deliveries(
            limit=self.policy.batch_size, claim_for=self.policy.claim_for
        )
        if not attempts:
            return 0
        async with self.client() as client:
            for attempt in attempts:
                started = time.monotonic()
                code: int | None = None
                error: str | None = None
                succeeded = False
                try:
                    secrets = self._active_secrets(attempt.endpoint_id, attempt.secret)
                    body = json.dumps(
                        attempt.event.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
                    ).encode()
                    timestamp = int(self.hub.clock.now().timestamp())
                    with masked_in_logs(*url_log_masks(attempt.url)):
                        response = await client.post(
                            attempt.url,
                            content=body,
                            headers={
                                "Content-Type": "application/json",
                                SIGNATURE_HEADER: make_signature(body, secrets, timestamp=timestamp),
                            },
                        )
                    code = response.status_code
                    succeeded = 200 <= code < 300
                    if not succeeded:
                        error = f"HTTP {code}"
                except Exception as exc:  # ошибка сети становится записью доставки, диспетчер живёт дальше
                    error = type(exc).__name__
                elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
                await self.hub._store.finish_webhook_delivery(
                    attempt.delivery_id,
                    attempt.attempt,
                    succeeded=succeeded,
                    response_code=code,
                    response_ms=elapsed_ms,
                    error=error,
                    retry_after=self.policy.retry_after(attempt.attempt),
                    max_failures=self.policy.max_failures,
                )
        return len(attempts)

    def _active_secrets(self, endpoint_id: UUID, payload: Any) -> list[SecretStr]:
        secrets = self.hub._envelope.open_model(
            _WebhookSecrets,
            payload,
            associated_data("ih_webhook_endpoints", endpoint_id),
        )
        result = [secrets.current]
        now = self.hub.clock.now()
        if secrets.previous is not None and secrets.previous_until is not None:
            until = secrets.previous_until
            if until.tzinfo is None:
                until = until.replace(tzinfo=UTC)
            if until >= now:
                result.append(secrets.previous)
        return result


def _matches_endpoint(endpoint: WebhookEndpointRow, event: Event, labels: Mapping[str, Any]) -> bool:
    if endpoint.space_slug != event.space:
        return False
    if not matches_any(endpoint.event_types, event.type):
        return False
    if not severity_at_least(str(event.severity), endpoint.min_severity):
        return False
    service_filter = endpoint.filter.get("service")
    if service_filter is not None:
        allowed = service_filter if isinstance(service_filter, list | tuple | set) else [service_filter]
        if event.service not in allowed:
            return False
    expected_labels = endpoint.filter.get("labels", {})
    if not isinstance(expected_labels, Mapping):
        return False
    for key, expected in expected_labels.items():
        allowed = expected if isinstance(expected, list | tuple | set) else [expected]
        if labels.get(str(key)) not in allowed:
            return False
    return True
