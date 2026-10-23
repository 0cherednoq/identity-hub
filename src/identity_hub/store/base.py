"""Порт хранилища (D-008).

Ядро работает с хранилищем только через `Store` и единицу работы `StoreTx`. PostgreSQL-реализация —
`identity_hub.postgres.store`, in-memory — `identity_hub.testing` (P10.1). Интерфейс растёт по мере
реализации задач: каждая задача добавляет абстрактные методы и их реализацию в PgStore.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from identity_hub.clock import Clock
from identity_hub.crypto.envelope import StoredPayload
from identity_hub.domain.regions import RegionState
from identity_hub.enums import LeaseEndReason
from identity_hub.errors import SecretConfigurationError
from identity_hub.events import Event, EventDraft
from identity_hub.rollups import BucketSize, RollupDelta, RollupRow
from identity_hub.snapshots import StateSnapshotFrame

NOTIFY_CHANNEL = "ih_events"
"""Канал NOTIFY внешних событий: будит диспетчер при коммите (FR-13.6)."""

__all__ = [
    "NOTIFY_CHANNEL",
    "NO_LEASE_OPTIONS",
    "PICK_DEMOTE_CHUNK",
    "ROTATION_TABLES",
    "UNREADABLE_IMPORT_ITEM",
    "AcceptanceRow",
    "AttentionChallengeRow",
    "AttentionIdentityRow",
    "CandidateQuery",
    "ChallengeRow",
    "ChargeOutcome",
    "DataRow",
    "DeliveryEnqueue",
    "DoctorIssue",
    "EventReadFilter",
    "EventReadRow",
    "IdempotencyClaim",
    "IdempotencyRequest",
    "IdentityRow",
    "ImportItemResult",
    "ImportItemRow",
    "ImportRunRow",
    "LeaseCounters",
    "LeaseHistoryRow",
    "LeaseOptions",
    "LeaseReadRow",
    "LeaseRenewal",
    "LeaseRow",
    "LeaseStatsRow",
    "LinkRow",
    "MetricsSnapshot",
    "NewCredentialRow",
    "NewIdentity",
    "NewIdentityRows",
    "NewImportItem",
    "NewImportRun",
    "NewNotificationChannel",
    "NewNotificationRule",
    "NewProxy",
    "NewSessionRow",
    "NewWebhookEndpoint",
    "NotificationAttempt",
    "NotificationChannelRow",
    "NotificationChannelSecretRow",
    "NotificationDeliveryReadRow",
    "NotificationEnqueue",
    "NotificationRuleRow",
    "NotificationTargetRow",
    "OperationSnapshot",
    "OverviewRequirementRow",
    "OverviewServiceRow",
    "OverviewSnapshot",
    "ProviderOperationRow",
    "ProxyBindingHistoryRow",
    "ProxyBoundIdentityRow",
    "ProxyPoolCapacityRow",
    "ProxyReadRow",
    "ProxyRow",
    "QuotaCharge",
    "QuotaWindow",
    "RotationBatch",
    "SearchFilter",
    "SecretConversionBatch",
    "ServiceRow",
    "SpaceRef",
    "Store",
    "StoreTx",
    "UnreadableSecret",
    "VerificationRow",
    "WebhookAttempt",
    "WebhookDeliveryRow",
    "WebhookEndpointReadRow",
    "WebhookEndpointRow",
    "YieldRequest",
    "expiry_reason",
]


@dataclass(frozen=True, slots=True)
class SpaceRef:
    id: UUID
    slug: str
    title: str
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class ImportRunRow:
    id: UUID
    space_id: UUID
    service: str
    actor: str
    source: str
    state: str
    on_existing: str
    verify_sessions: bool
    labels: dict[str, Any]
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    total: int
    processed: int
    created: int
    updated: int
    skipped: int
    failed: int
    report: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NewImportRun:
    id: UUID
    service: str
    actor: str
    source: str
    on_existing: str
    verify_sessions: bool
    labels: dict[str, Any]
    total: int


@dataclass(frozen=True, slots=True)
class NewImportItem:
    position: int
    payload: StoredPayload


@dataclass(frozen=True, slots=True)
class ImportItemRow:
    run_id: UUID
    position: int
    claim_token: int
    payload: StoredPayload


@dataclass(frozen=True, slots=True)
class ImportItemResult:
    position: int
    claim_token: int
    outcome: str
    identity_id: UUID | None = None
    external_ref: str | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AcceptanceRow:
    identity_id: UUID
    space_id: UUID
    policy: dict[str, Any]
    attempts: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderOperationRow:
    id: UUID
    space_id: UUID
    identity_id: UUID
    kind: str
    state: str
    attempt: int
    claim_id: UUID | None
    claim_token: int
    claimed_by: str | None
    claim_expires_at: datetime | None
    available_at: datetime
    deadline_at: datetime | None
    result: dict[str, Any]
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    revision: int = 1  # каждое изменение записи + 1 (D-220)


@dataclass(frozen=True, slots=True)
class ProviderClaim:
    """Claim операции провайдера (IMP-11, D-220): применить результат можно только с ним.

    `claim_id` — ключ идемпотентности для адаптера; `claim_token` растёт с каждым claim.
    """

    operation_id: UUID
    identity_id: UUID
    kind: str
    attempt: int
    claim_id: UUID
    claim_token: int
    claim_expires_at: datetime
    deadline_at: datetime | None


@dataclass(frozen=True, slots=True)
class DoctorIssue:
    kind: str
    space: str
    entity_id: UUID | str
    expected: Any
    actual: Any


ROTATION_TABLES: tuple[str, ...] = (
    "credentials",
    "sessions",
    "challenges",
    "verifications",
    "proxies",
    "webhooks",
    "notification_channels",
    "profiles",
    "import_items",
)
"""Имена наборов с шифртекстом, которые обходит ротация мастер-ключа (S-3)."""

UNREADABLE_IMPORT_ITEM = "staged_payload_unreadable"
"""Код элемента импорта, который не удалось расшифровать: строка сохраняется, прогон — `failed`."""


@dataclass(frozen=True, slots=True)
class UnreadableSecret:
    """Строка, которую не удалось расшифровать при ротации: набор и первичный ключ через `/`."""

    table: str
    id: str


@dataclass(frozen=True, slots=True)
class RotationBatch:
    """Одна пачка ротации набора. `cursor is None` — набор пройден до конца."""

    rotated: int
    unreadable: tuple[UnreadableSecret, ...] = ()
    cursor: tuple[Any, ...] | None = None


@dataclass(frozen=True, slots=True)
class SecretConversionBatch:
    """Одна атомарная пачка перевода credentials/session между форматами хранения."""

    converted: int
    spaces: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    cursor_lag: dict[str, int]
    delivery_queue: dict[str, int]
    cursor_updated_at: dict[str, datetime] = field(default_factory=dict)
    delivery_failed: dict[str, int] = field(default_factory=dict)
    delivery_next_attempt_at: dict[str, datetime | None] = field(default_factory=dict)
    delivery_last_success_at: dict[str, datetime | None] = field(default_factory=dict)
    delivery_has_error: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OverviewServiceRow:
    """Aggregated, non-secret current facts for one service in a space."""

    service: str
    total: int
    eligible: int
    leased: int
    heavy: int
    open_challenges: int
    action_required: int
    added_24h: int
    added_7d: int


@dataclass(frozen=True, slots=True)
class OverviewRequirementRow:
    service: str
    code: str
    count: int
    next_deadline: datetime | None


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    """Storage aggregate consumed by the public overview read model."""

    services: tuple[OverviewServiceRow, ...]
    factor_sets: tuple[tuple[str, tuple[str, ...], int], ...]
    verification_states: tuple[tuple[str, str, str, int], ...]
    requirements: tuple[OverviewRequirementRow, ...]


@dataclass(frozen=True, slots=True)
class AttentionChallengeRow:
    id: UUID
    kind: str
    hint: str
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class AttentionIdentityRow:
    id: UUID
    service: str
    external_ref: str
    availability: str
    error_score: float
    requirements: tuple[dict[str, Any], ...]
    regions: tuple[tuple[str, str, RegionState], ...]
    challenge: AttentionChallengeRow | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ServiceRow:
    name: str
    title: str
    url: str | None = None
    verification_tiers: tuple[frozenset[str], ...] = ()


@dataclass(frozen=True, slots=True)
class IdentityRow:
    """Строка ih_identities без секретов."""

    id: UUID
    space_id: UUID
    service: str
    external_ref: str
    lifecycle: str
    standing: str
    standing_code: str | None
    standing_until: datetime | None
    auth_state: str
    availability: str
    availability_until: datetime | None
    requirements: list[Any]
    tags: list[str]
    attributes: dict[str, Any]
    profile: dict[str, Any]
    labels: dict[str, Any]
    note: str | None
    search_text: str
    verified_factors: list[str]
    error_score: float
    fencing_seq: int
    event_seq: int
    created_at: datetime
    updated_at: datetime
    normalized_ref: str = ""  # Service.normalize_ref(external_ref), IMP-08
    normalize_version: int = 0
    revision: int = 0  # число команд, изменивших агрегат (IMP-07, D-205)
    last_leased_at: datetime | None = None  # последний захват аренды (IMP-01, D-209)
    # место в порядке выборки LRU: захват ставит время, исчерпанная квота переносит в хвост (N1.3b)
    pick_order_at: datetime | None = None
    yield_reserved_priority: int | None = None
    yield_reserved_until: datetime | None = None
    verification_tier_rank: int | None = None


@dataclass(frozen=True, slots=True)
class NewIdentity:
    """Вставка аккаунта: данные уже провалидированы, секреты зашифрованы."""

    id: UUID
    service: str
    external_ref: str
    lifecycle: str
    standing: str
    auth_state: str
    tags: list[str]
    attributes: dict[str, Any]
    profile: dict[str, Any]
    labels: dict[str, Any]
    note: str | None
    search_text: str
    profile_secret: StoredPayload | None = None
    normalized_ref: str = ""  # пусто — `external_ref.strip()` (нормализация по умолчанию)
    normalize_version: int = 0

    @property
    def unique_ref(self) -> str:
        """Ключ уникальности `(service, unique_ref)` (IMP-08)."""
        return self.normalized_ref or self.external_ref.strip()


@dataclass(frozen=True, slots=True)
class DataRow:
    """Строка ih_identity_data."""

    data: dict[str, Any]
    schema_version: int
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LinkRow:
    """Связь `identity_id --role--> linked_identity_id` с данными другой стороны для read-моделей."""

    identity_id: UUID
    role: str
    linked_identity_id: UUID
    other_service: str
    other_external_ref: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateQuery:
    """Условия выборки аккаунта для аренды (ТЗ FR-6.1, §3.9)."""

    service: str
    standing: frozenset[str]
    needs_auth: bool = True
    required_factors: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    labels: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    among: Any = None  # список id или подзапрос домена (Select) — FR-6.1
    exclude: frozenset[UUID] = frozenset()
    shared: bool = False  # FR-6.6: живая аренда не исключает аккаунт
    proxy_wait: bool = False  # FR-8.3 on_proxy_down=wait: аккаунт с прокси не в alive недоступен
    # sticky-пул: аккаунт, привязанный к выключенному прокси, недоступен при любой политике (D-224)
    proxy_enabled: bool = False
    lifecycles: frozenset[str] = frozenset({"ready"})
    auth_states: frozenset[str] | None = None  # задано — вместо needs_auth (выборка для входа, FR-5.4)
    # окна квот операции: аккаунт с исчерпанным текущим окном не выдаётся (require_quota, N1.3)
    quotas: tuple[QuotaCharge, ...] = ()
    priority: int = 0  # учитывает короткую бронь после кооперативной уступки (N3.2b)
    without_link: frozenset[str] = frozenset()
    not_target_of: tuple[str, str | None] | None = None
    is_target_of: tuple[str, str | None] | None = None


@dataclass(frozen=True, slots=True)
class LeaseRow:
    """Живая аренда (ih_leases) или, в shared-режиме, только строка истории."""

    lease_id: UUID
    token: UUID = field(repr=False)  # приватный: в repr и логах не появляется (FR-6.4)
    identity_id: UUID
    fencing: int
    pool: str
    holder: str
    acquired_at: datetime
    expires_at: datetime
    idempotency_key: str | None = None
    proxy_id: UUID | None = None
    heartbeat_at: datetime | None = None
    purpose: str | None = None  # метка вида работы, по ней группируют
    ref: str | None = None  # ссылка на задачу держателя
    hold_deadline: datetime | None = None  # потолок удержания: продление за него не выходит
    priority: int = 0
    yield_requested_at: datetime | None = None
    yield_requested_priority: int | None = None
    throttle_state: dict[str, datetime] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class LeaseOptions:
    """Необязательные свойства новой аренды, которые хранятся в её строке и в истории."""

    purpose: str | None = None
    ref: str | None = None
    max_hold_seconds: float | None = None  # хранилище превращает в `hold_deadline` от своего времени
    priority: int = 0


def expiry_reason(hold_deadline: datetime | None, expired_at: datetime) -> LeaseEndReason:
    """Причина для аренды, чей срок вышел: на своём потолке удержания или просто без heartbeat."""
    if hold_deadline is not None and hold_deadline <= expired_at:
        return LeaseEndReason.MAX_HOLD
    return LeaseEndReason.EXPIRED


NO_LEASE_OPTIONS = LeaseOptions()
# Перенос исчерпанных аккаунтов из головы порядка выборки (N1.3b): столько строк головы
# переносит один захват. 1024 строк достаточно, чтобы прогреть худший случай на 50k аккаунтов
# за 50 захватов, но работа одного запроса остаётся ограниченной независимо от размера парка.
PICK_DEMOTE_CHUNK = 1024

RELEASE_OUTCOMES = frozenset({"released", "expired", "closed", "revoked"})


@dataclass(frozen=True, slots=True)
class NewCredentialRow:
    """Запись credentials новой identity: секрет уже зашифрован (`I6.3`)."""

    kind: str
    name: str
    payload: StoredPayload
    public_value: dict[str, Any] | None = None
    expires_at: datetime | None = None
    value_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class NewSessionRow:
    """Сессия слота новой identity: секрет уже зашифрован (`I6.3`)."""

    slot: str
    payload: StoredPayload
    expires_at: datetime | None = None
    value_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class NewIdentityRows:
    """Новая identity со всеми дочерними строками — для пакетной вставки (`I6.3`, `PERF-1`).

    Регионы и связи задаются готовыми значениями: вставка не читает состояние и не делает
    запрос на строку.
    """

    identity: NewIdentity
    credentials: StoredPayload | None = None
    credential_fingerprint: str | None = None
    credential_records: tuple[NewCredentialRow, ...] = ()
    sessions: tuple[NewSessionRow, ...] = ()
    regions: tuple[tuple[str, str, RegionState], ...] = ()
    links: tuple[tuple[str, UUID], ...] = ()


@dataclass(frozen=True, slots=True)
class OperationSnapshot:
    """Состояние аккаунта для входа в операцию, снятое одним запросом (`I6.1`, `PERF-1`).

    Собирает то, что раньше читалось четырьмя: проверку fence, строку аккаунта, подтверждённые
    факторы с истёкшим сроком и время базы.
    """

    identity: IdentityRow
    expired_factors: tuple[str, ...]
    now: datetime


@dataclass(frozen=True, slots=True)
class LeaseFence:
    """Кому принадлежит аренда (IMP-05, D-212): запись от её имени принимается при полном совпадении.

    `token` — секрет аренды, в repr не попадает.
    """

    identity_id: UUID
    lease_id: UUID
    token: UUID = field(repr=False)
    fencing: int
    shared: bool = False


@dataclass(slots=True)
class LeaseCounters:
    """Приращения счётчиков истории аренды с прошлого сброса (FR-6.7)."""

    operations: dict[str, int]
    succeeded: int = 0
    failed: int = 0
    last_error: str | None = None

    @classmethod
    def empty(cls) -> LeaseCounters:
        return cls(operations={})

    @property
    def is_empty(self) -> bool:
        return not self.operations and not self.succeeded and not self.failed and self.last_error is None


@dataclass(frozen=True, slots=True)
class LeaseRenewal:
    """Ответ продления аренды (heartbeat).

    `expires_at` — новый срок живой аренды; `None` — аренды с этим token больше нет. Тогда
    `end_reason` говорит, чем она кончилась: причина из закрытой истории, `expired` или `max_hold`
    — срок вышел, а строку ещё никто не забрал, `None` — строки нет, а история открыта или неизвестна.
    """

    expires_at: datetime | None
    end_reason: LeaseEndReason | None = None
    yield_requested: bool = False


@dataclass(frozen=True, slots=True)
class YieldRequest:
    """Результат запроса уступить: цель нужна для немедленной доставки внутри процесса."""

    lease_id: UUID
    forced: bool = False


@dataclass(frozen=True, slots=True)
class QuotaCharge:
    """Одно окно квоты: вид операции, длина окна, начало отсчёта, лимит (FR-7.2)."""

    kind: str
    window_seconds: int
    origin: datetime
    limit: int


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    kind: str
    window_seconds: int
    window_start: datetime
    used: int
    # лимит из правила операции сервиса; заполняется в карточке, `None` — правила с таким окном нет
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyRequest:
    """Заявка на ключ идемпотентности (IMP-03): ключ живёт в `namespace` до `ttl`."""

    namespace: str  # пространство и семейство команд: `<space_id>:operation`
    key: str
    operation: str  # имя команды; у операции под арендой — её вид
    fingerprint: str  # `domain.idempotency.fingerprint`: секретов не содержит
    ttl: timedelta
    owner_identity_id: UUID | None = None
    # команда с секретами (D-208): ключ отпечатков и отпечатки той же команды прежними ключами
    fingerprint_key_id: str | None = None
    alternates: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def fingerprint_for(self, key_id: str | None) -> str:
        """Отпечаток этой команды ключом записи; нет ключа у живой записи — `SecretConfigurationError`."""
        if key_id is None or key_id == self.fingerprint_key_id:
            return self.fingerprint
        found = dict(self.alternates).get(key_id)
        if found is None:
            raise SecretConfigurationError("нет ключа отпечатков, которым подписан ключ идемпотентности")
        return found

    @property
    def candidates(self) -> list[str]:
        return [self.fingerprint, *(value for _key, value in self.alternates)]


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    """Итог заявки. `claimed` — ключ занят этим вызовом: первая попытка либо повтор после `failed`."""

    claimed: bool
    attempt: int
    previous_outcome: str | None = None  # "succeeded" | "failed" | None (нет или неизвестен)
    owner_identity_id: UUID | None = None
    result_type: str | None = None
    result_version: int | None = None
    result: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ChargeOutcome:
    """Результат списания: `duplicate` — ключ операции уже был (FR-7.5); `exhausted` — окно без единиц."""

    duplicate: bool = False
    exhausted: QuotaWindow | None = None
    reached: tuple[QuotaWindow, ...] = ()  # окна, израсходованные этой операцией до конца
    claim: IdempotencyClaim | None = None  # заполнено, если списание шло с ключом идемпотентности

    @property
    def ok(self) -> bool:
        return self.exhausted is None


@dataclass(frozen=True, slots=True)
class NewProxy:
    id: UUID
    pool: str
    scheme: str
    host: str
    port: int
    fingerprint: str
    max_identities: int
    secret: StoredPayload
    fingerprint_key_id: str | None = None
    # отпечатки того же адреса и логина другими ключами кольца и прежний SHA-256 (D-207)
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProxyRow:
    """Строка ih_proxies; логин и пароль — в зашифрованном `secret`."""

    id: UUID
    space_id: UUID
    pool: str
    scheme: str
    host: str
    port: int
    fingerprint: str
    status: str
    error_score: float
    fail_streak: int
    next_check_at: datetime | None
    status_changed_at: datetime
    status_source: str
    checked_at: datetime | None
    pinned_until: datetime | None
    pending_observation: str | None
    max_identities: int
    created_at: datetime
    secret: StoredPayload
    fingerprint_key_id: str | None = None  # NULL — SHA-256 до миграции 0018
    # намерение оператора (D-224): выключенный прокси не выдаётся и не трогается автоматикой
    enabled: bool = True
    disabled_reason: str | None = None
    disabled_by: str | None = None
    disabled_at: datetime | None = None

    @property
    def issuable(self) -> bool:
        """Правило выдачи: прокси включён оператором и жив."""
        return self.enabled and self.status == "alive"


@dataclass(frozen=True, slots=True)
class ProxyReadRow:
    """Secret-free proxy projection for administrative reads."""

    id: UUID
    pool: str
    scheme: str
    host: str
    port: int
    status: str
    error_score: float
    fail_streak: int
    next_check_at: datetime | None
    status_changed_at: datetime
    status_source: str
    checked_at: datetime | None
    pinned_until: datetime | None
    pending_observation: str | None
    max_identities: int
    bound_count: int
    in_lease_count: int
    created_at: datetime
    enabled: bool = True
    disabled_reason: str | None = None
    disabled_by: str | None = None
    disabled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProxyBoundIdentityRow:
    identity_id: UUID
    service: str
    external_ref: str
    bound_at: datetime


@dataclass(frozen=True, slots=True)
class ProxyBindingHistoryRow:
    event_id: UUID
    identity_id: UUID | None
    service: str | None
    external_ref: str | None
    from_proxy_id: UUID | None
    to_proxy_id: UUID | None
    reason: str
    pool: str
    actor: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ProxyPoolCapacityRow:
    pool: str
    proxies: int
    alive: int
    quarantined: int
    dead: int
    other: int
    max_identities: int
    bound_count: int
    in_lease_count: int
    disabled: int = 0  # выключено оператором; статусы считаются по всем прокси группы


@dataclass(frozen=True, slots=True)
class EventReadFilter:
    allowed_types: frozenset[str]
    severities: frozenset[str] = frozenset()
    service: str | None = None
    identity_id: UUID | None = None
    proxy_id: UUID | None = None
    lease_id: UUID | None = None
    actor: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class EventReadRow:
    event_id: UUID
    type: str
    severity: str
    service: str | None
    identity_id: UUID | None
    proxy_id: UUID | None
    lease_id: UUID | None
    sequence: int | None
    actor: str
    data: Mapping[str, Any]
    external: bool
    occurred_at: datetime
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class ChallengeRow:
    id: UUID
    identity_id: UUID
    kind: str
    hint: str
    context: StoredPayload
    answer_nonce: bytes | None
    answer_ciphertext: bytes | None
    answer_payload: str | None
    created_at: datetime
    expires_at: datetime | None
    answered_at: datetime | None
    resolved_at: datetime | None


@dataclass(frozen=True, slots=True)
class VerificationRow:
    """Данные фактора в ih_verifications (состояние — в ih_region_states)."""

    attempts: int
    started_at: datetime | None
    submitted_at: datetime | None
    verified_at: datetime | None
    expires_at: datetime | None
    evidence: StoredPayload | None
    via_identity_id: UUID | None = None
    details: dict[str, Any] = field(default_factory=dict)


def credential_label(identity_id: object, kind: str, name: str) -> str:
    """Как назвать запись credentials в отчётах: `default`/`default` — id аккаунта, как до 0022."""
    return str(identity_id) if (kind, name) == ("default", "default") else f"{identity_id}/{kind}/{name}"


@dataclass(frozen=True, slots=True)
class CredentialRow:
    """Запись credentials без секрета (IMP-09, D-214): вид, имя, открытая часть, жизненный цикл."""

    id: UUID
    identity_id: UUID
    kind: str
    name: str
    public_value: dict[str, Any] | None
    lifecycle: str
    expires_at: datetime | None
    revision: int
    version: int
    created_at: datetime
    updated_at: datetime
    value_fingerprint: str | None = None
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    reject_code: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialMetadataRow:
    present: bool
    version: int | None
    updated_at: datetime | None
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    reject_code: str | None = None


@dataclass(frozen=True, slots=True)
class SessionWrite:
    """Итог записи сессии (D-218): запись слота, её ревизия и отозванная предшественница при replace."""

    id: UUID
    revision: int
    replaced_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class SessionHistoryRow:
    """Запись сессии слота без секрета — текущая или отозванная."""

    id: UUID
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None
    value_fingerprint: str | None = None
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    reject_code: str | None = None


@dataclass(frozen=True, slots=True)
class SessionMetadataRow:
    slot: str
    status: str
    expires_at: datetime | None
    version: int
    written_by_fencing: int
    updated_at: datetime | None
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    reject_code: str | None = None


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """Разрешённый поисковый запрос (FR-11.1–11.4). Внутри списков — ИЛИ, между полями — И."""

    services: frozenset[str] = frozenset()
    regions: dict[str, frozenset[str]] = field(default_factory=dict)  # колонка → значения
    any_region: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
    contains: tuple[tuple[str, tuple[dict[str, Any], ...]], ...] = ()  # (profile|labels, варианты @>)
    terms: tuple[str, ...] = ()
    exact_id: UUID | None = None
    short_id: str | None = None
    attention: bool | None = None
    eligible: bool | None = None
    leased: bool | None = None
    has_override: bool | None = None
    requirement: str | None = None
    factor: str | None = None
    factor_state: str | None = None
    tier_factors: dict[str, frozenset[str]] = field(default_factory=dict)
    sort_tiers: dict[str, tuple[frozenset[str], ...]] = field(default_factory=dict)
    proxy: str | None = None
    linked: bool | None = None
    # ждут проверки: есть действующая запись без исхода проверки (C8.2)
    credential_unverified: bool | None = None
    session_unverified: bool | None = None
    without_link: frozenset[str] = frozenset()
    not_target_of: tuple[str, str | None] | None = None
    is_target_of: tuple[str, str | None] | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    last_leased_from: datetime | None = None
    last_leased_to: datetime | None = None
    synced_from: datetime | None = None
    synced_to: datetime | None = None
    standing_changed_from: datetime | None = None
    standing_changed_to: datetime | None = None
    sort_by: str = "created_at"
    sort_desc: bool = True


@dataclass(frozen=True, slots=True)
class LeaseHistoryRow:
    """Строка ih_lease_history без token."""

    lease_id: UUID
    identity_id: UUID
    fencing: int
    pool: str
    holder: str
    proxy_id: UUID | None
    acquired_at: datetime
    last_heartbeat_at: datetime
    released_at: datetime | None
    duration: timedelta | None
    end_reason: str | None
    operations: dict[str, int]
    succeeded: int
    failed: int
    last_error: str | None
    purpose: str | None = None
    ref: str | None = None
    hold_deadline: datetime | None = None
    priority: int = 0


@dataclass(frozen=True, slots=True)
class LeaseReadRow:
    """Secret-free space-level lease fact used by operational read models."""

    lease_id: UUID
    identity_id: UUID
    service: str
    external_ref: str
    fencing: int
    pool: str
    holder: str
    proxy_id: UUID | None
    acquired_at: datetime
    last_heartbeat_at: datetime
    released_at: datetime | None
    duration: timedelta | None
    end_reason: str | None
    operations: dict[str, int]
    succeeded: int
    failed: int
    last_error: str | None
    expires_at: datetime | None
    purpose: str | None = None
    ref: str | None = None
    hold_deadline: datetime | None = None
    priority: int = 0


@dataclass(frozen=True, slots=True)
class LeaseStatsRow:
    count: int
    total: timedelta
    average: timedelta | None
    longest: timedelta | None
    by_reason: dict[str, int]
    operations: dict[str, int]
    succeeded: int
    failed: int


@dataclass(frozen=True, slots=True)
class NewWebhookEndpoint:
    id: UUID
    url: str
    description: str | None
    event_types: list[str]
    filter: dict[str, Any]
    min_severity: str
    secret: StoredPayload
    ordered: bool = False  # порядок внутри потока аккаунта (IMP-21, D-328)


@dataclass(frozen=True, slots=True)
class WebhookEndpointRow:
    id: UUID
    space_id: UUID
    space_slug: str
    url: str
    description: str | None
    event_types: list[str]
    filter: dict[str, Any]
    min_severity: str
    enabled: bool
    disabled_reason: str | None
    failure_streak: int
    created_at: datetime
    secret: StoredPayload
    ordered: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryEnqueue:
    """Доставка к постановке: получатель, событие и поток ресурса (`D-328`).

    `resource_key` — аккаунт события, `resource_revision` — его ревизия; `None` означает, что
    событие не входит в упорядоченный поток и порядок на него не распространяется.
    """

    endpoint_id: UUID
    event_id: UUID
    resource_key: str | None = None
    resource_revision: int | None = None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryRow:
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
    resource_key: str | None = None
    resource_revision: int | None = None


@dataclass(frozen=True, slots=True)
class WebhookEndpointReadRow:
    """Secret-free endpoint projection used by administration reads."""

    id: UUID
    url: str
    description: str | None
    event_types: list[str]
    filter: dict[str, Any]
    min_severity: str
    enabled: bool
    disabled_reason: str | None
    failure_streak: int
    created_at: datetime
    ordered: bool = False


@dataclass(frozen=True, slots=True)
class WebhookAttempt:
    delivery_id: int
    attempt: int
    endpoint_id: UUID
    url: str
    secret: StoredPayload
    event: Event


@dataclass(frozen=True, slots=True)
class NewNotificationChannel:
    id: UUID
    title: str
    config: dict[str, Any]
    secret: StoredPayload


@dataclass(frozen=True, slots=True)
class NotificationChannelRow:
    id: UUID
    title: str
    config: dict[str, Any]
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationChannelSecretRow:
    id: UUID
    config: dict[str, Any]
    secret: StoredPayload


@dataclass(frozen=True, slots=True)
class NewNotificationRule:
    id: UUID
    channel_id: UUID
    name: str
    event_types: list[str]
    min_severity: str
    filter: dict[str, Any]
    digest_seconds: int | None
    template: str | None
    created_by: str


@dataclass(frozen=True, slots=True)
class NotificationRuleRow:
    id: UUID
    channel_id: UUID
    name: str
    enabled: bool
    event_types: list[str]
    min_severity: str
    filter: dict[str, Any]
    digest_seconds: int | None
    template: str | None
    created_by: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationTargetRow:
    rule: NotificationRuleRow
    space_slug: str
    channel_config: dict[str, Any]
    channel_secret: StoredPayload


@dataclass(frozen=True, slots=True)
class NotificationEnqueue:
    rule_id: UUID
    channel_id: UUID
    event_id: UUID
    digest_key: str | None
    scheduled_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationAttempt:
    notification_id: int
    attempt: int
    rule_id: UUID
    channel_id: UUID
    template: str | None
    channel_config: dict[str, Any]
    channel_secret: StoredPayload
    events: tuple[Event, ...]


@dataclass(frozen=True, slots=True)
class NotificationDeliveryReadRow:
    """Space-scoped notification journal row without channel secrets."""

    id: int
    rule_id: UUID
    rule_name: str
    channel_id: UUID
    channel_title: str
    event_ids: list[UUID]
    status: str
    attempt: int
    scheduled_at: datetime
    sent_at: datetime | None
    error: str | None


class StoreTx(ABC):
    """Единица работы в рамках одного пространства (своя транзакция или savepoint пользователя)."""

    space: SpaceRef

    @abstractmethod
    async def now(self) -> datetime:
        """Время БД (`now()`), единое для всей транзакции (ТЗ §8.6)."""

    @abstractmethod
    async def emit(self, drafts: Sequence[EventDraft]) -> list[UUID]:
        """Записать события в журнал в этой транзакции (outbox, FR-13.1, G-4.1).

        `data` уже очищены от секретов. Для событий аккаунта инкрементирует `event_seq`
        и проставляет `sequence`. Внешние события будят диспетчер (NOTIFY при коммите).
        """

    @abstractmethod
    def savepoint(self) -> AbstractAsyncContextManager[None]:
        """Точка сохранения внутри транзакции (`D-224`): исключение в блоке откатывает только
        записи и события блока и пробрасывается дальше; остальная транзакция продолжается."""

    # --------------------------------------------------------- аккаунт и регионы (P2.3)

    @abstractmethod
    async def get_identities(self, identity_ids: Sequence[UUID]) -> list[IdentityRow]:
        """Аккаунты этого пространства по списку id одним запросом; чужие и неизвестные пропускаются."""

    @abstractmethod
    async def get_identity(self, identity_id: UUID) -> IdentityRow | None:
        """Аккаунт этого пространства (чужое пространство — None)."""

    @abstractmethod
    async def lock_identity_revision(self, identity_id: UUID, expected_revision: int) -> int:
        """Lock an identity and atomically verify its event revision for an admin command."""

    @abstractmethod
    async def update_identity(
        self, identity_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        """Обновить колонки ih_identities; `expected` — compare-and-set (D-107). False — не совпало."""

    @abstractmethod
    async def load_region(self, identity_id: UUID, region: str, key: str) -> RegionState | None: ...

    @abstractmethod
    async def load_regions(
        self, identity_ids: Sequence[UUID], region: str, key: str
    ) -> dict[UUID, RegionState]:
        """Пакетно прочитать строки одного региона без N+1."""
        ...

    @abstractmethod
    async def save_region(
        self, identity_id: UUID, region: str, key: str, previous: RegionState | None, new: RegionState
    ) -> bool:
        """Записать регион с проверкой `version` (previous=None — вставка). False — гонка."""

    # --------------------------------------------------------- регистрация (P2.4)

    @abstractmethod
    async def find_identity(
        self, identity_id: UUID, service: str, normalized_ref: str
    ) -> tuple[UUID, UUID] | None:
        """(id, space_id) аккаунта по id или (service, normalized_ref) во всех пространствах (FR-2.2)."""

    @abstractmethod
    async def insert_identities(self, rows: Sequence[NewIdentityRows]) -> list[UUID]:
        """Вставить несколько identity с дочерними строками одной серией SQL (`I6.3`).

        Возвращает id вставленных в порядке входа; занятый id пропускается, его дочерние строки
        не пишутся. Регионы, credentials, сессии и связи вставляются только вставленным.
        """
        raise NotImplementedError

    @abstractmethod
    async def insert_identity(self, row: NewIdentity) -> bool:
        """False — конфликт уникальности (параллельная регистрация)."""

    @abstractmethod
    async def find_by_refs(self, refs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], tuple[UUID, UUID]]:
        """(service, normalized_ref) → (id, space_id) — во всех пространствах (импорт, FR-2.2, IMP-08)."""

    @abstractmethod
    async def existing_identities(self, ids: Sequence[UUID]) -> set[UUID]:
        """Какие из аккаунтов есть в этом пространстве."""

    @abstractmethod
    async def put_credentials(
        self,
        identity_id: UUID,
        payload: StoredPayload,
        *,
        value_fingerprint: str | None = None,
    ) -> None:
        """Запись `default`/`default` (прежний единственный blob, D-214)."""

    @abstractmethod
    async def put_credential(
        self,
        identity_id: UUID,
        kind: str,
        name: str,
        payload: StoredPayload,
        *,
        public_value: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
        value_fingerprint: str | None = None,
    ) -> CredentialRow:
        """Создать или заменить запись (identity, kind, name): секрет, открытая часть и срок вместе,
        `revision` и `version` + 1 у существующей. Жизненный цикл не меняет (D-214)."""

    @abstractmethod
    async def get_credential(
        self, identity_id: UUID, kind: str, name: str
    ) -> tuple[StoredPayload, CredentialRow] | None: ...

    @abstractmethod
    async def set_credential_lifecycle(
        self, identity_id: UUID, kind: str, name: str, lifecycle: str
    ) -> CredentialRow | None:
        """Сменить жизненный цикл записи, `revision` + 1; `None` — записи нет (D-215)."""

    @abstractmethod
    async def list_credentials(self, identity_id: UUID) -> list[CredentialRow]:
        """Записи credentials аккаунта пространства без секретов, по (kind, name)."""

    @abstractmethod
    async def mark_credential_validity(
        self,
        identity_id: UUID,
        kind: str,
        name: str,
        *,
        rejected: bool,
        code: str | None = None,
        disable: bool = False,
    ) -> CredentialRow | None:
        """Атомарно пометить конкретную запись проверенной или отклонённой."""

    @abstractmethod
    async def credential_candidate_state(self, identity_id: UUID) -> bool | None:
        """True — есть активная пара; False — записи есть, но кандидатов нет; None — записей нет."""

    @abstractmethod
    async def put_session(
        self,
        identity_id: UUID,
        slot: str,
        payload: StoredPayload,
        *,
        fencing: int,
        expires_at: datetime | None = None,
        mode: str = "in_place",
        value_fingerprint: str | None = None,
    ) -> SessionWrite | None:
        """Записать сессию слота (G-1.4, IMP-10, D-218). `None` — запись более новой аренды не перезаписать.

        `in_place` обновляет не отозванную запись слота (revision + 1) или создаёт её; `replace`
        отзывает её и вставляет новую с revision + 1 — история слота сохраняется.
        """

    @abstractmethod
    async def revoke_session(self, identity_id: UUID, slot: str) -> bool:
        """Отозвать не отозванную запись слота навсегда; `False` — отзывать нечего."""

    @abstractmethod
    async def session_history(self, identity_id: UUID, slot: str) -> list[SessionHistoryRow]:
        """Все записи слота без секретов по времени создания."""

    @abstractmethod
    async def mark_session_validity(
        self,
        identity_id: UUID,
        slot: str,
        *,
        rejected: bool,
        code: str | None = None,
    ) -> bool:
        """Пометить текущую сессию слота проверенной или отклонённой."""

    @abstractmethod
    async def insert_link(self, identity_id: UUID, role: str, linked_identity_id: UUID) -> bool:
        """False — связь с этой ролью уже есть (FR-4.1)."""

    # --------------------------------------------------------- данные аккаунта (P2.5)

    @abstractmethod
    async def get_profile_secret(self, identity_id: UUID) -> StoredPayload | None: ...

    @abstractmethod
    async def set_profile_secret(self, identity_id: UUID, payload: StoredPayload | None) -> None: ...

    @abstractmethod
    async def incoming_links(self, identity_id: UUID) -> list[tuple[UUID, str]]:
        """(зависимый аккаунт, роль) для связей, указывающих на этот аккаунт."""

    @abstractmethod
    async def delete_identity(self, identity_id: UUID) -> bool: ...

    @abstractmethod
    async def load_data(self, identity_id: UUID, namespace: str) -> DataRow | None: ...

    @abstractmethod
    async def save_data(
        self,
        identity_id: UUID,
        namespace: str,
        data: dict[str, Any],
        schema_version: int,
        expected_version: int | None,
    ) -> int | None:
        """Новая версия или None при конфликте (expected_version=None — только вставка новой строки)."""

    # --------------------------------------------------------- связи (P2.6)

    @abstractmethod
    async def delete_link(self, identity_id: UUID, role: str) -> UUID | None:
        """Удалить связь; вернуть linked_identity_id или None, если связи не было."""

    @abstractmethod
    async def links_of(self, identity_id: UUID) -> tuple[list[LinkRow], list[LinkRow]]:
        """(исходящие, входящие); `other_*` — данные противоположной стороны."""

    @abstractmethod
    async def regions_for(self, ids: Sequence[UUID]) -> dict[UUID, list[tuple[str, str, RegionState]]]:
        """Регионы нескольких аккаунтов одним запросом (`I6.2`, `R2-D08`)."""
        ...

    @abstractmethod
    async def current_leases_for(self, ids: Sequence[UUID]) -> dict[UUID, LeaseRow]:
        """Живые аренды нескольких аккаунтов одним запросом (`I6.2`)."""
        ...

    @abstractmethod
    async def bindings_for(self, ids: Sequence[UUID]) -> dict[UUID, ProxyRow]:
        """Привязанные прокси нескольких аккаунтов одним запросом (`I6.2`)."""
        ...

    @abstractmethod
    async def last_lease_for(self, ids: Sequence[UUID]) -> dict[UUID, LeaseHistoryRow]:
        """Последняя запись истории аренд каждого аккаунта одним запросом (`I6.2`)."""
        ...

    @abstractmethod
    async def quota_usage_for(self, ids: Sequence[UUID]) -> dict[UUID, list[QuotaWindow]]:
        """Действующие окна квот нескольких аккаунтов одним запросом (`I6.2`)."""
        ...

    @abstractmethod
    async def link_counts(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int, list[str]]]:
        """id → (исходящих, входящих, роли исходящих) — одним запросом для страницы списка."""

    @abstractmethod
    async def dependents(self, identity_id: UUID, max_depth: int) -> list[tuple[UUID, int, list[str]]]:
        """Транзитивно зависящие аккаунты: (id, глубина, цепочка ролей) — анализ последствий."""

    # --------------------------------------------------------- аренда (P3.x)

    @abstractmethod
    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
        """Свободный допустимый аккаунт, заблокированный до конца транзакции (FOR UPDATE SKIP LOCKED).

        Порядок — наименее недавно арендованный (D-007). При `query.quotas` аккаунты с исчерпанной
        квотой из головы порядка сначала переносятся в хвост (N1.3b). Истёкшие временные состояния
        availability считаются доступными (ленивое снятие, ТЗ §3.4).
        """

    @abstractmethod
    async def pick_candidate_reserving(
        self, query: CandidateQuery, reserve: int
    ) -> tuple[IdentityRow | None, bool]:
        """Заблокировать до ``reserve + 1`` кандидатов; вернуть (первый, reserve_blocked)."""

    @abstractmethod
    async def request_yield(
        self, query: CandidateQuery, priority: int, grace_seconds: float | None = None
    ) -> YieldRequest | None:
        """Попросить одну живую допустимую аренду с меньшим priority кооперативно уступить."""

    @abstractmethod
    async def count_candidates(self, query: CandidateQuery, limit: int) -> int:
        """Сколько аккаунтов сейчас можно выдать под запрос (до `limit`) — ёмкость пула."""

    @abstractmethod
    async def count_live_candidates(self, query: CandidateQuery, limit: int) -> int:
        """Сколько подходящих аккаунтов сейчас имеют живую аренду."""

    @abstractmethod
    async def quota_reopens_at(self, query: CandidateQuery) -> datetime | None:
        """Когда откроется квота у ближайшего из аккаунтов, которых отсекает только `query.quotas`.

        Для аккаунта это конец самого позднего из исчерпанных окон; ответ — минимум по аккаунтам.
        `None` — таких аккаунтов нет: отказ вызван не квотой. Считается только на пути отказа.
        """

    @abstractmethod
    async def find_live_lease(self, idempotency_key: str) -> LeaseRow | None:
        """Живая аренда с этим ключом идемпотентности (FR-6.5)."""

    @abstractmethod
    async def acquire_lease(
        self,
        identity: IdentityRow,
        *,
        pool: str,
        holder: str,
        ttl_seconds: float,
        idempotency_key: str | None,
        shared: bool,
        proxy_id: UUID | None = None,
        options: LeaseOptions = NO_LEASE_OPTIONS,
    ) -> tuple[LeaseRow, LeaseRow | None]:
        """Захватить аренду заблокированного кандидата: fencing + 1, ih_leases, строка истории (G-4.2).

        Истёкшая аренда перезаписывается, её история закрывается как `expired` временем `expires_at`
        (FR-6.8). Возвращает (новая аренда, перезахваченная истёкшая или None).
        """

    @abstractmethod
    async def extend_lease(
        self,
        lease: LeaseRow,
        ttl_seconds: float,
        counters: LeaseCounters,
        *,
        shared: bool,
        flush: bool = True,
    ) -> LeaseRenewal:
        """Heartbeat: продлить живую аренду по token. Аренды больше нет — ответ без `expires_at`.

        `flush` — записать в историю время heartbeat и `counters`; без него эксклюзивная аренда
        обновляет только `ih_leases` (HOT, D-210). Разделяемая пишет историю всегда.
        """

    @abstractmethod
    async def release_lease(
        self, lease: LeaseRow, reason: str, counters: LeaseCounters, *, shared: bool
    ) -> str:
        """Освободить аренду по token и закрыть историю (G-4.2, D-213). Исход — `RELEASE_OUTCOMES`.

        `released` — своя живая аренда закрыта с `reason`; `expired` — срок вышел, аккаунт никто не
        взял: история закрыта как `expired` на момент `expires_at`; `closed` — история уже закрыта
        (повтор или перехват после истечения); `revoked` — аренду отозвали.
        """

    @abstractmethod
    async def get_throttle_state(self, identity_id: UUID, names: Sequence[str]) -> dict[str, datetime]:
        """Сохранённые TAT для именованных троттлов identity."""

    @abstractmethod
    async def save_throttle_state(self, identity_id: UUID, state: Mapping[str, datetime]) -> None:
        """Сохранить TAT монотонно (greatest), не укорачивая состояние другого держателя."""

    @abstractmethod
    async def last_unclean_lease_end(self, identity_id: UUID) -> datetime | None:
        """Конец последней истёкшей/потерянной аренды для консервативного восстановления TAT."""

    @abstractmethod
    async def take_shared_throttle(
        self, identity_id: UUID, name: str, interval_seconds: float, burst: int
    ) -> float:
        """Атомарно занять общий GCRA-слот или вернуть секунды ожидания, не меняя TAT."""

    @abstractmethod
    async def lock_identities(self, identity_ids: Sequence[UUID]) -> None:
        """Эксклюзивно (FOR NO KEY UPDATE) заблокировать identity этого пространства по возрастанию id.

        Единый порядок блокировок (IMP-14): транзакция, меняющая несколько identity, сначала
        вызывает этот метод, и параллельные транзакции ждут друг друга вместо deadlock.
        """

    @abstractmethod
    async def check_fence(self, fence: LeaseFence, *, lock: bool = True) -> None:
        """Аренда жива и принадлежит владельцу fence, иначе `LeaseOwnershipLost` (IMP-05, D-212).

        Эксклюзивная: строка `ih_leases` с теми же `lease_id` и `token`, `expires_at > now()` и
        `fencing_seq == fencing`; разделяемая — открытая история с теми же `lease_id` и `token`.
        `lock=True` держит строку identity (`FOR NO KEY UPDATE`) до конца транзакции.
        """
        raise NotImplementedError

    @abstractmethod
    async def operation_snapshot(self, fence: LeaseFence) -> OperationSnapshot:
        """Fence, аккаунт, истёкшие факторы и время базы одним round trip (`I6.1`).

        Проверка та же, что у `check_fence(lock=True)`: несовпадение номера, чужая или истёкшая
        аренда — `LeaseOwnershipLost`; аккаунта нет — `NotFound`.
        """
        raise NotImplementedError

    @abstractmethod
    async def current_fencing(self, identity_id: UUID, *, lock: bool = False) -> int | None:
        """Текущий fencing_seq аккаунта; `lock=True` — FOR SHARE до конца транзакции."""

    @abstractmethod
    async def revoke_lease(self, identity_id: UUID) -> LeaseRow | None:
        """Отозвать живую аренду: fencing + 1, строка ih_leases освобождена, история закрыта как revoked."""

    # --------------------------------------------------------- квоты (P4.1)

    @abstractmethod
    async def charge_quotas(
        self,
        identity_id: UUID,
        charges: Sequence[QuotaCharge],
        *,
        idempotency: IdempotencyRequest | None = None,
    ) -> ChargeOutcome:
        """Атомарно списать по всем окнам (savepoint, G-2.2) и занять ключ идемпотентности (D-114).

        Ключ уже занят — `duplicate=True`, квота не списана. Исчерпанное окно ключ не занимает.
        Тот же ключ с другой командой — `IdempotencyConflict`.
        """

    @abstractmethod
    async def bump_pacing(self, identity_id: UUID, charge: QuotaCharge) -> bool:
        """+1 к счётчику pacing; True — достигнут порог (счётчик обнулён), нужна пауза (D-113)."""

    @abstractmethod
    async def quota_usage(self, identity_id: UUID) -> list[QuotaWindow]:
        """Окна квот аккаунта, актуальные на now() (для карточки и проверки допустимости)."""

    # --------------------------------------------------------- идемпотентность (IMP-03)

    @abstractmethod
    async def claim_idempotency_key(self, request: IdempotencyRequest) -> IdempotencyClaim:
        """Занять ключ в `request.namespace` либо вернуть сведения о прошлой попытке.

        Ключ занимается, если его нет, он истёк или прошлая попытка закончилась `failed`.
        Незакоммиченный победитель удерживает конкурента; его откат освобождает ключ.
        Другая команда под тем же ключом — `IdempotencyConflict`.
        """

    @abstractmethod
    async def complete_idempotency_key(
        self,
        namespace: str,
        key: str,
        outcome: str,
        *,
        result_type: str | None = None,
        result_version: int | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        """Записать исход попытки (`succeeded` | `failed`) и типизированный результат для replay."""

    # --------------------------------------------------------- здоровье аккаунта (P4.2)

    @abstractmethod
    async def add_error_score(self, identity_id: UUID, delta: float) -> float:
        """error_score += delta (не ниже 0); вернуть новое значение."""

    @abstractmethod
    async def decay_error_score(self, identity_id: UUID, factor: float) -> None:
        """error_score *= factor, только если он > 0 (без лишней записи на каждую успешную операцию)."""

    @abstractmethod
    async def set_session_status(self, identity_id: UUID, slot: str, status: str) -> None: ...

    # --------------------------------------------------------- прокси (P5.1)

    @abstractmethod
    async def insert_proxy(self, row: NewProxy) -> tuple[UUID, bool]:
        """(id, создан). Дубликат по fingerprint или `aliases` в пространстве — существующий id (FR-8.1)."""

    @abstractmethod
    async def insert_proxies(self, rows: Sequence[NewProxy]) -> list[tuple[ProxyRow, bool]]:
        """Вставить пачку прокси; результат — в порядке входа: (строка, создана ли этой записью).

        Адрес, который уже есть в пространстве или встретился в пачке раньше, даёт существующую
        строку и `False`. Число запросов не зависит от размера пачки.
        """

    @abstractmethod
    async def get_proxy(self, proxy_id: UUID) -> ProxyRow | None: ...

    @abstractmethod
    async def update_proxy(
        self, proxy_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        """Обновить колонки прокси; `expected` — compare-and-set."""

    @abstractmethod
    async def get_binding(self, identity_id: UUID) -> ProxyRow | None:
        """Прокси, к которому привязан аккаунт (sticky, FR-8.2)."""

    @abstractmethod
    async def add_proxy_error(self, proxy_id: UUID, weight: float) -> float | None:
        """Атомарно прибавить `weight` к `error_score` живого прокси и вернуть новое значение.

        `None` — прокси нет или он не `alive`: счёт ошибок ведётся только у живых.
        """

    @abstractmethod
    async def proxy_by_fingerprint(self, fingerprints: Sequence[str]) -> UUID | None:
        """Прокси пространства с любым из отпечатков (теги разными ключами кольца, D-207)."""

    @abstractmethod
    async def set_proxy_secret(
        self, proxy_id: UUID, fingerprint: str, secret: StoredPayload, *, fingerprint_key_id: str | None
    ) -> bool:
        """Заменить логин и пароль прокси (шифртекст) вместе с отпечатком."""

    @abstractmethod
    async def set_proxy_fingerprint(self, proxy_id: UUID, fingerprint: str, key_id: str | None) -> bool:
        """Записать пересчитанный отпечаток; `False` — прокси нет или отпечаток занят другим прокси."""

    @abstractmethod
    async def unbind_identity(self, identity_id: UUID) -> UUID | None:
        """Снять привязку аккаунта и освободить место на прокси; вернуть id прокси или `None`."""

    @abstractmethod
    async def unbind_proxy(self, proxy_id: UUID) -> list[UUID]:
        """Снять все привязки аккаунтов к прокси; вернуть id отвязанных аккаунтов."""

    @abstractmethod
    async def delete_proxy(self, proxy_id: UUID) -> list[UUID] | None:
        """Удалить прокси вместе с привязками; вернуть id отвязанных аккаунтов. `None` — прокси нет."""

    @abstractmethod
    async def choose_proxy(self, pool: str, max_identities: int | None, *, claim: bool) -> ProxyRow | None:
        """Наименее загруженный живой прокси пула с запасом ёмкости.

        `claim=True` — атомарно занять единицу ёмкости (`bound_count + 1`, D-117) для sticky-привязки.
        """

    @abstractmethod
    async def bind_proxy(self, identity_id: UUID, proxy_id: UUID, previous: UUID | None) -> None:
        """Привязать аккаунт к прокси; ёмкость прежнего прокси освобождается."""

    @abstractmethod
    async def rebind_proxy(self, identity_id: UUID, proxy_id: UUID) -> tuple[UUID | None, ProxyRow] | None:
        """Atomically move one sticky binding when the target is alive and has capacity.

        The identity row serializes competing rebinds. ``None`` means that the target
        disappeared, became unhealthy or filled up before it could be claimed.
        """

    @abstractmethod
    async def adjust_proxy_load(self, proxy_id: UUID, delta: int) -> None:
        """bound_count += delta (не ниже 0) — привязка вне выбора прокси (импорт)."""

    @abstractmethod
    async def due_proxies(self, limit: int, *, pool: str | None = None) -> list[UUID]:
        """Прокси, которым пора на перепроверку: `quarantined` и `dead` с наступившим `next_check_at`.

        `dead` без `next_check_at` (записи до F3.7a) считаются созревшими. Закреплённые прокси
        (`pinned_until` в будущем) не возвращаются. `pool` — только эта группа.
        """

    @abstractmethod
    async def proxy_read_page(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
        pool: str | None,
        status: str | None,
        capacity: str | None,
        enabled: bool | None = None,
    ) -> list[ProxyReadRow]: ...

    @abstractmethod
    async def proxy_read_one(self, proxy_id: UUID) -> ProxyReadRow | None: ...

    @abstractmethod
    async def proxy_bound_identities(self, proxy_id: UUID) -> list[ProxyBoundIdentityRow]: ...

    @abstractmethod
    async def proxy_pool_capacities(self) -> list[ProxyPoolCapacityRow]: ...

    @abstractmethod
    async def proxy_binding_history(
        self,
        proxy_id: UUID,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> list[ProxyBindingHistoryRow]: ...

    # --------------------------------------------------------- журнал событий (P8.1)

    @abstractmethod
    async def event_read_page(
        self,
        filters: EventReadFilter,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> list[EventReadRow]: ...

    @abstractmethod
    async def event_read_one(self, event_id: UUID) -> EventReadRow | None: ...

    # --------------------------------------------------------- верификации (P5.4)

    @abstractmethod
    async def load_verification(self, identity_id: UUID, factor: str) -> VerificationRow | None: ...

    @abstractmethod
    async def save_verification(
        self, identity_id: UUID, factor: str, values: dict[str, Any], evidence: StoredPayload | None = None
    ) -> None:
        """Upsert строки ih_verifications; `evidence` — перезаписать доказательства."""

    @abstractmethod
    async def due_regions(self, region: str, limit: int) -> list[tuple[UUID, str]]:
        """(identity_id, key) строк региона, у которых истёк `until` (снятие временных состояний)."""

    @abstractmethod
    async def expired_factors(self, identity_id: UUID) -> list[str]:
        """Факторы в `verified`, чей срок действия (`until`) уже вышел по времени хранилища."""

    @abstractmethod
    async def stale_identities(
        self, service: str, region: str, key: str, older_than_seconds: float, limit: int
    ) -> list[UUID]:
        """Кандидаты фоновой сверки: `ready`-аккаунты сервиса, чей регион не сверялся дольше порога.

        Порядок — по времени последней попытки (ни разу не пробовали — первыми), затем по времени
        последней сверки и id: аккаунт со стабильно падающей пробой не занимает начало очереди.
        """

    @abstractmethod
    async def mark_probe_attempts(self, identity_ids: Sequence[UUID]) -> None:
        """Отметить попытку фоновой сверки временем хранилища — при любом её исходе."""

    # --------------------------------------------------------- поиск (P6.1)

    @abstractmethod
    async def tag_counts(self, *, service: str | None, limit: int) -> list[tuple[str, int]]:
        """Теги аккаунтов пространства с числом аккаунтов: по убыванию числа, затем по тегу."""

    @abstractmethod
    async def search_identities(
        self,
        flt: SearchFilter,
        *,
        limit: int,
        after: tuple[Any, UUID] | None,
        count_limit: int | None,
    ) -> tuple[list[IdentityRow], int | None]:
        """Страница (created_at DESC, id DESC) и ограниченный счётчик (до count_limit + 1)."""

    # --------------------------------------------------------- логи аренды (P7.2)

    @abstractmethod
    async def insert_lease_logs(self, rows: Sequence[dict[str, Any]]) -> None:
        """Пачка записей ih_lease_logs: lease_id, identity_id, ts, level, message, fields."""

    # --------------------------------------------------------- read-модели (P6.2)

    @abstractmethod
    async def list_regions(self, identity_id: UUID) -> list[tuple[str, str, RegionState]]: ...

    @abstractmethod
    async def current_lease(self, identity_id: UUID) -> LeaseRow | None:
        """Живая аренда аккаунта (без показа token наружу — это забота вызывающего)."""

    @abstractmethod
    async def data_namespaces(self, identity_id: UUID) -> list[tuple[str, int, int, datetime]]:
        """(namespace, schema_version, version, updated_at)."""

    @abstractmethod
    async def region_counts(self, service: str | None) -> list[tuple[str, str, str, int]]:
        """(service, колонка региона, значение, число аккаунтов) — сводка пула (FR-11.6)."""

    @abstractmethod
    async def overview_snapshot(self) -> OverviewSnapshot:
        """Агрегаты текущего space без выдачи строк аккаунтов наружу."""

    @abstractmethod
    async def attention_rows(self) -> list[AttentionIdentityRow]:
        """Secret-free facts for identities that may require human attention."""

    @abstractmethod
    async def lease_history(
        self, identity_id: UUID, *, limit: int, before: datetime | None
    ) -> list[LeaseHistoryRow]: ...

    @abstractmethod
    async def lease_stats(self, identity_id: UUID, since: datetime) -> LeaseStatsRow: ...

    @abstractmethod
    async def lease_read_rows(self, since: datetime) -> list[LeaseReadRow]:
        """Lease facts touching ``since..now`` plus open rows, without tokens."""

    @abstractmethod
    async def lease_read_page(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
        pool: str | None,
        service: str | None,
        holder: str | None,
    ) -> list[LeaseReadRow]:
        """Stable page ordered by ``(acquired_at, lease_id) DESC``."""

    @abstractmethod
    async def identities_page(self, service: str, after: UUID | None, limit: int) -> list[IdentityRow]:
        """Страница аккаунтов сервиса по id (переиндексация)."""

    # --------------------------------------------------------- challenge (P5.3)

    @abstractmethod
    async def open_challenge(
        self,
        challenge_id: UUID,
        identity_id: UUID,
        kind: str,
        hint: str,
        context: StoredPayload,
        expires_at: datetime | None,
    ) -> bool:
        """False — у аккаунта уже открыт challenge (FR-5.5, уникальный частичный индекс)."""

    @abstractmethod
    async def get_challenge(self, challenge_id: UUID) -> ChallengeRow | None: ...

    @abstractmethod
    async def open_challenge_of(self, identity_id: UUID) -> ChallengeRow | None: ...

    @abstractmethod
    async def answer_challenge(
        self,
        challenge_id: UUID,
        nonce: bytes | None = None,
        ciphertext: bytes | None = None,
        *,
        plaintext: str | None = None,
    ) -> bool:
        """Сохранить зашифрованный ответ; False — challenge уже закрыт."""

    @abstractmethod
    async def resolve_challenge(self, challenge_id: UUID) -> bool:
        """Закрыть challenge и стереть шифртекст ответа; False — уже закрыт."""

    @abstractmethod
    async def expired_challenges(self, limit: int) -> list[ChallengeRow]: ...

    # --------------------------------------------------------- секреты (P3.4)

    @abstractmethod
    async def get_credentials(self, identity_id: UUID) -> StoredPayload | None: ...

    @abstractmethod
    async def get_session(self, identity_id: UUID, slot: str) -> tuple[StoredPayload, str] | None:
        """(payload, status) сессии слота."""

    @abstractmethod
    async def access_metadata(
        self, identity_id: UUID
    ) -> tuple[CredentialMetadataRow, list[SessionMetadataRow]]:
        """Только metadata доступов; encrypted payload намеренно не возвращается."""

    # --------------------------------------------------------- исходящие вебхуки (P8.3)

    @abstractmethod
    async def insert_webhook_endpoint(self, row: NewWebhookEndpoint) -> WebhookEndpointRow: ...

    @abstractmethod
    async def get_webhook_endpoint(self, endpoint_id: UUID) -> WebhookEndpointRow | None: ...

    @abstractmethod
    async def update_webhook_secret(self, endpoint_id: UUID, secret: StoredPayload) -> bool: ...

    @abstractmethod
    async def update_webhook_endpoint(
        self,
        endpoint_id: UUID,
        *,
        url: str,
        description: str | None,
        event_types: list[str],
        filter: dict[str, Any],
        min_severity: str,
        ordered: bool | None = None,
    ) -> bool: ...

    @abstractmethod
    async def delete_webhook_endpoint(self, endpoint_id: UUID) -> bool: ...

    @abstractmethod
    async def set_webhook_enabled(self, endpoint_id: UUID, enabled: bool, reason: str | None) -> bool: ...

    @abstractmethod
    async def retry_webhook_delivery(self, delivery_id: int) -> bool:
        """Вернуть доставку в pending и включить её endpoint (явное действие администратора)."""

    @abstractmethod
    async def webhook_deliveries(self, endpoint_id: UUID, limit: int) -> list[WebhookDeliveryRow]: ...

    @abstractmethod
    async def webhook_endpoint_reads(self) -> list[WebhookEndpointReadRow]: ...

    @abstractmethod
    async def blocked_webhook_deliveries(self) -> dict[UUID, int]:
        """Сколько доставок каждого `ordered`-получателя ждут предыдущую ревизию (`D-328`)."""
        ...

    @abstractmethod
    async def webhook_delivery_read_page(
        self, *, limit: int, before: tuple[datetime, int] | None = None
    ) -> list[WebhookDeliveryRow]: ...

    # --------------------------------------------------------- Telegram-уведомления (P8.4)

    @abstractmethod
    async def insert_notification_channel(self, row: NewNotificationChannel) -> NotificationChannelRow: ...

    @abstractmethod
    async def notification_channels(self) -> list[NotificationChannelRow]: ...

    @abstractmethod
    async def get_notification_channel_secret(
        self, channel_id: UUID
    ) -> NotificationChannelSecretRow | None: ...

    @abstractmethod
    async def update_notification_channel(
        self, channel_id: UUID, values: dict[str, Any], secret: StoredPayload | None
    ) -> bool: ...

    @abstractmethod
    async def delete_notification_channel(self, channel_id: UUID) -> bool: ...

    @abstractmethod
    async def insert_notification_rule(self, row: NewNotificationRule) -> NotificationRuleRow: ...

    @abstractmethod
    async def notification_rules(self) -> list[NotificationRuleRow]: ...

    @abstractmethod
    async def notification_delivery_read_page(
        self, *, limit: int, before: tuple[datetime, int] | None = None
    ) -> list[NotificationDeliveryReadRow]: ...

    @abstractmethod
    async def retry_notification_delivery(self, delivery_id: int) -> bool: ...

    @abstractmethod
    async def update_notification_rule(self, rule_id: UUID, values: dict[str, Any]) -> bool: ...

    @abstractmethod
    async def delete_notification_rule(self, rule_id: UUID) -> bool: ...

    # --------------------------------------------------------- admin durable state (A4.1)

    @abstractmethod
    async def get_import_run(self, run_id: UUID) -> ImportRunRow | None:
        """Return a secret-free import run scoped to this transaction's space."""

    @abstractmethod
    async def list_import_runs(self, *, limit: int = 100) -> list[ImportRunRow]:
        """Newest import runs, with redacted reports and without staged payloads."""

    @abstractmethod
    async def create_import_run(self, run: NewImportRun, items: Sequence[NewImportItem]) -> bool:
        """Atomically create a run and its encrypted, resumable staging rows."""

    @abstractmethod
    async def active_import_runs(self, *, limit: int = 10) -> list[ImportRunRow]: ...

    @abstractmethod
    async def claim_import_items(
        self,
        run_id: UUID,
        *,
        worker: str,
        limit: int,
        claim_for: timedelta,
    ) -> list[ImportItemRow]:
        """Claim one committed batch; expired claims become eligible again."""

    @abstractmethod
    async def finish_import_items(
        self, run_id: UUID, results: Sequence[ImportItemResult]
    ) -> ImportRunRow | None:
        """Fence item results, advance counters and finalize or cancel at a batch boundary."""

    @abstractmethod
    async def request_import_cancel(self, run_id: UUID) -> ImportRunRow | None:
        """Stop new claims and cancel now or after the currently claimed batch."""

    @abstractmethod
    async def enqueue_acceptance(self, identity_ids: Sequence[UUID], policy: dict[str, Any]) -> int:
        """Mark imported identities for acceptance in one bulk store operation (D-317)."""

    @abstractmethod
    async def list_acceptance(self, *, limit: int = 100) -> list[AcceptanceRow]:
        """Oldest pending acceptance entries in this transaction's space."""

    @abstractmethod
    async def update_acceptance(
        self, identity_id: UUID, *, attempts: int | None = None, done: bool = False
    ) -> bool:
        """Advance an acceptance entry or remove it after a terminal outcome."""

    @abstractmethod
    async def enqueue_provider_operation(
        self,
        identity_id: UUID,
        kind: str,
        *,
        available_at: datetime | None = None,
        deadline_at: datetime | None = None,
    ) -> ProviderOperationRow:
        """Поставить операцию в очередь: `pending`, `attempt = 0`, `revision = 1` (D-220).

        Живая (`pending`/`running`) операция того же вида у аккаунта уже есть — `AlreadyExists` (D-223).
        """
        raise NotImplementedError

    @abstractmethod
    async def claim_provider_operations(
        self,
        worker: str,
        *,
        limit: int,
        claim_for: timedelta,
        kinds: frozenset[str] | None = None,
        operation_ids: Sequence[UUID] | None = None,
    ) -> list[ProviderClaim]:
        """Взять доступные `pending`/`retryable` операции пространства (`SKIP LOCKED`): `running`,
        `attempt + 1`, новый `claim_id`, `claim_token + 1`, срок claim. Коммит — до вызова адаптера."""

    @abstractmethod
    async def apply_provider_operation(
        self,
        claim: ProviderClaim,
        outcome: str,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        policy: Any = None,
        fence: LeaseFence | None = None,
    ) -> bool:
        """Применить итог (`succeeded`/`retryable`/`failed`) по claim; `False` — поздний ответ.

        Принимается только при совпадении `claim_id`/`claim_token`, состоянии `running` и
        неистёкшем claim; `fence` — дополнительно полный lease fence (D-212). `retryable` по
        `policy` (`RetryPolicy`) может стать `failed`: попытки исчерпаны или вышел срок.
        """

    @abstractmethod
    async def recover_provider_operations(self, *, limit: int, policy: Any = None) -> int:
        """`running` с истёкшим claim → `retryable` (или `failed` по `policy`); число восстановленных."""

    @abstractmethod
    async def cancel_provider_operation(self, operation_id: UUID) -> bool:
        """`pending`/`retryable` → `cancelled`; `False` — операция уже выполняется или завершена."""

    @abstractmethod
    async def list_provider_operations(
        self,
        *,
        identity_id: UUID | None = None,
        states: frozenset[str] | None = None,
        limit: int = 100,
    ) -> list[ProviderOperationRow]:
        """Durable provider operation state; result must never contain secrets."""

    # --------------------------------------------------------- обслуживание (P9.1)

    @abstractmethod
    async def due_identity_timers(self, limit: int) -> list[tuple[UUID, str]]:
        """Истёкшие денормализованные standing/availability таймеры (закреплённый standing — нет).

        Только состояния, у которых есть переход по истечении (`RegionSpec.on_expire`): `until`
        у `frozen` или `quarantined` — справочная дата, такая строка в очередь не попадает.
        """

    @abstractmethod
    async def stuck_logins(self, limit: int) -> list[tuple[UUID, datetime]]:
        """Аккаунты пространства в `auth=logging_in` без живой аренды: `(id, с какого момента)`.

        Вход держит аренду с heartbeat; `logging_in` без аренды — след упавшего процесса.
        """

    @abstractmethod
    async def expire_leases(self, limit: int) -> int:
        """Закрыть историю истёкших аренд временем expires_at и записать lease.expired."""


class Store(ABC):
    def bind_clock(self, clock: Clock | None) -> Clock | None:
        """Согласовать часы с хабом; возвращает часы, по которым должен работать хаб.

        Хранилище с временем базы данных своих часов не имеет и возвращает `clock` как есть.
        """
        return clock

    @abstractmethod
    def transaction(
        self, space: SpaceRef, *, session: Any | None = None
    ) -> AbstractAsyncContextManager[StoreTx]:
        """Своя транзакция библиотеки, либо savepoint в сессии пользователя (ТЗ T-1, T-3)."""

    @abstractmethod
    async def schema_revisions(self) -> tuple[str, list[str]]:
        """(ревизия схемы, которую ожидает библиотека, фактические ревизии хранилища) — IMP-15.

        Фактических несколько — разветвлённая история; пусто — схема не создана.
        """

    @abstractmethod
    async def current_time(self) -> datetime:
        """Текущее время хранилища: по нему считаются границы сроков хранения и корзины (R2-D20)."""

    def compatible_revisions(self, expected: str) -> frozenset[str]:
        """Ревизии схемы, на которых работает код библиотеки; по умолчанию — только ожидаемая."""
        return frozenset({expected})

    @abstractmethod
    async def sync_services(self, services: Sequence[ServiceRow]) -> None:
        """Зеркало реестра сервисов для админки (FR-1.3)."""

    @abstractmethod
    async def ensure_space(
        self,
        slug: str,
        title: str | None = None,
        space_id: UUID | None = None,
        *,
        timezone: str = "UTC",
    ) -> SpaceRef:
        """Получить или создать пространство."""

    @abstractmethod
    async def get_space(self, slug: str) -> SpaceRef | None: ...

    @abstractmethod
    async def list_spaces(self, slugs: frozenset[str] | None = None) -> list[SpaceRef]:
        """Пространства, отфильтрованные до выполнения дочерних агрегатов."""

    # --------------------------------------------------------- журнал событий (P8.1)

    @abstractmethod
    async def process_events(
        self,
        consumer: str,
        handler: Callable[[list[Event]], Awaitable[int | None]],
        *,
        limit: int,
        external_only: bool = True,
        start_now: bool = False,
    ) -> int | None:
        """Обработать пачку внешних событий потребителя (G-5.2, G-5.5).

        Строка курсора блокируется `FOR UPDATE SKIP LOCKED` на время пачки (None — занята другим
        экземпляром). Читаются события по `(tx_id, id)` строго после курсора и ниже горизонта
        `pg_snapshot_xmin`. Курсор продвигается только если `handler` завершился без ошибки.
        `handler` может вернуть число обработанных событий с начала пачки: курсор встаёт за
        последним из них, остальные придут снова; `None` — обработана вся пачка. Возвращается
        число событий, за которые продвинут курсор.

        `start_now` действует только при создании курсора: он встаёт на текущий горизонт
        журнала, так что потребитель не получает историю, но получает события транзакций,
        ещё открытых в этот момент.
        """

    @abstractmethod
    async def rollup_source_events(self, space_slug: str, start: datetime, end: datetime) -> list[Event]:
        """Read committed internal and external events in an occurrence-time range."""

    @abstractmethod
    async def replace_rollups(self, rows: Sequence[RollupRow]) -> None:
        """Upsert absolute values for rollup keys; repeating the call is a no-op."""

    @abstractmethod
    async def apply_metric_rollup_batch(self, batch_id: UUID, rows: Sequence[RollupDelta]) -> bool:
        """Apply a buffered metric batch exactly once and return whether it was new."""

    @abstractmethod
    async def event_rollup_rows(
        self, space_id: UUID, bucket_size: BucketSize, start: datetime, end: datetime
    ) -> list[RollupRow]:
        """Корзины событий пространства заданного размера в `[start, end)`, без `telemetry.*`."""

    @abstractmethod
    async def clear_rollups(self, space_id: UUID, start: datetime, end: datetime) -> int: ...

    @abstractmethod
    async def purge_minute_rollups(self, before: datetime) -> int: ...

    @abstractmethod
    async def read_rollups(
        self,
        space_slug: str,
        *,
        metric: str,
        bucket_size: BucketSize,
        start: datetime,
        end: datetime,
        service: str | None,
        dims: tuple[tuple[str, str], ...] | None,
    ) -> list[RollupRow]: ...

    @abstractmethod
    async def capture_state_snapshot(self, taken_at: datetime, *, source: str) -> int: ...

    @abstractmethod
    async def read_state_snapshots(
        self, space_slug: str, start: datetime, end: datetime
    ) -> list[StateSnapshotFrame]: ...

    @abstractmethod
    async def purge_state_snapshots(self, before: datetime) -> int: ...

    @abstractmethod
    def listen(self, channel: str) -> AbstractAsyncContextManager[asyncio.Event | None]:
        """Подписка на NOTIFY: событие взводится при уведомлении; None — LISTEN недоступен (опрос)."""

    # --------------------------------------------------------- исходящие вебхуки (P8.3)

    @abstractmethod
    async def enabled_webhook_endpoints(self) -> list[WebhookEndpointRow]: ...

    @abstractmethod
    async def identity_labels(self, identity_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]: ...

    @abstractmethod
    async def enqueue_webhook_deliveries(self, deliveries: Sequence[DeliveryEnqueue]) -> int: ...

    @abstractmethod
    async def claim_webhook_deliveries(self, *, limit: int, claim_for: timedelta) -> list[WebhookAttempt]:
        """Коротко зарезервировать due-доставки; сетевой I/O выполняется после коммита."""

    @abstractmethod
    async def release_webhook_deliveries(
        self, claims: Sequence[tuple[int, int]], *, retry_after: timedelta
    ) -> int:
        """Вернуть захваченные, но не отправленные доставки: `(delivery_id, attempt)` захвата.

        Попытка не засчитывается (`attempt − 1`), следующая — через `retry_after`. Доставка, у
        которой номер попытки уже другой или статус не `pending`/`failed`, не трогается.
        Возвращает число возвращённых доставок.
        """

    @abstractmethod
    async def finish_webhook_delivery(
        self,
        delivery_id: int,
        attempt: int,
        *,
        succeeded: bool,
        response_code: int | None,
        response_ms: int,
        error: str | None,
        retry_after: timedelta,
        max_failures: int,
    ) -> bool:
        """Зафиксировать попытку; вернуть False, если резервирование уже устарело."""

    # --------------------------------------------------------- Telegram-уведомления (P8.4)

    @abstractmethod
    async def enabled_notification_targets(self) -> list[NotificationTargetRow]: ...

    @abstractmethod
    async def enqueue_notifications(self, items: Sequence[NotificationEnqueue]) -> int: ...

    @abstractmethod
    async def claim_notifications(self, *, limit: int, claim_for: timedelta) -> list[NotificationAttempt]: ...

    @abstractmethod
    async def finish_notification(
        self,
        notification_id: int,
        attempt: int,
        *,
        succeeded: bool,
        error: str | None,
        retry_after: timedelta,
        max_attempts: int,
        pause_channel: timedelta | None = None,
    ) -> bool:
        """Записать исход попытки.

        `pause_channel` — получатель ответил 429: попытка не засчитывается (`attempt − 1`),
        уведомление возвращается в очередь через `pause_channel`, а канал до этого момента не
        захватывается. `False` — попытка уже завершена или захвачена заново.
        """

    # --------------------------------------------------------- обслуживание (P9.1)

    @abstractmethod
    async def maintenance_spaces(self) -> list[SpaceRef]: ...

    @abstractmethod
    async def cleanup_maintenance(
        self, retention: Mapping[str, timedelta], names: frozenset[str], *, batch_size: int = 1000
    ) -> dict[str, int]:
        """Удалить по времени хранилища данные категорий `names`; вернуть число строк по категориям.

        Категории: `quota_windows` (завершившиеся окна), `operation_keys` (истёкшие ключи),
        `lease_logs`, `lease_history` (закрытые аренды), `webhook_deliveries`, `notifications`,
        `events`, `import_runs`, `provider_operations` (завершённые), `metric_batches`,
        `closed_challenges`, `rollup_hours`, `rollup_days`. Срок берётся из `retention` по имени
        категории; у `webhook_deliveries` и `notifications` общий срок `deliveries`, у
        `quota_windows` срока нет.

        Вместе с событиями удаляются их доставки вебхуков; они входят в счёт `webhook_deliveries`.
        Удаление идёт пачками не больше `batch_size` строк, каждая — в своей транзакции: сбой
        посреди чистки не откатывает уже удалённое, а долгая транзакция не держит блокировки.
        """

    # --------------------------------------------------------- doctor / rotate-key (P9.2)

    @abstractmethod
    async def doctor(self, *, fix: bool, event_retention: timedelta | None = None) -> list[DoctorIssue]: ...

    @abstractmethod
    async def rotate_key(
        self, envelope: Any, *, table: str, limit: int, after: tuple[Any, ...] | None = None
    ) -> RotationBatch:
        """Переобернуть до `limit` строк набора `table` (из `ROTATION_TABLES`) после курсора `after`.

        Строки под неактивным ключом берутся по возрастанию первичного ключа; заблокированные
        другой транзакцией пропускаются (их подберёт следующий проход), нерасшифровываемые
        не меняются и возвращаются в `unreadable`.
        """

    @abstractmethod
    async def inactive_key_usage(self, active: str) -> dict[str, dict[str, int]]:
        """Набор → `key_id` → число строк с шифртекстом под ключом, отличным от `active`."""

    @abstractmethod
    async def convert_secret_storage(
        self,
        envelope: Any,
        *,
        table: str,
        to: str,
        limit: int,
        space: str | None = None,
    ) -> SecretConversionBatch:
        """Атомарно перевести до `limit` строк credentials/session, не меняя доменные ревизии."""

    @abstractmethod
    async def plaintext_secret_counts(self, *, space: str | None = None) -> dict[str, int]:
        """Число открытых строк credentials/session; нулевые наборы не возвращаются."""

    @abstractmethod
    async def stale_proxy_fingerprints(
        self, active_key_id: str, limit: int, after: UUID | None
    ) -> list[tuple[str, ProxyRow]]:
        """(slug, прокси) с отпечатком не активным ключом или без ключа, по возрастанию id после `after`."""

    @abstractmethod
    async def rekey_secret_value_fingerprints(
        self,
        envelope: Any,
        fingerprints: Any,
        *,
        table: str,
        limit: int,
        after: tuple[Any, ...] | None = None,
    ) -> RotationBatch:
        """Пересчитать HMAC значения credentials/session постранично; секрет наружу не отдавать."""

    @abstractmethod
    async def renew_leases(self, tokens: Sequence[UUID], ttl_seconds: float) -> dict[UUID, LeaseRenewal]:
        """Пачка heartbeat (IMP-02, D-211): продлить живые эксклюзивные аренды по token одним запросом.

        Возвращает ответ на каждый token; у аренды, которой нет или срок вышел, `expires_at` пуст.
        Историю не трогает.
        """

    @abstractmethod
    async def stale_normalizations(self, service: str, version: int, limit: int) -> list[tuple[UUID, str]]:
        """(id, external_ref) identity сервиса с `normalize_version != version`, по возрастанию id."""

    @abstractmethod
    async def apply_normalizations(
        self, service: str, version: int, updates: Sequence[tuple[UUID, str]]
    ) -> list[list[UUID]]:
        """Записать новые `normalized_ref` одной транзакцией (IMP-08).

        Если после записи значения совпали бы между собой или с уже нормализованными identity
        сервиса, ничего не меняется и возвращаются группы id коллизий; иначе — пустой список.
        """

    # --------------------------------------------------------- метрики (P9.3)

    @abstractmethod
    async def metrics_snapshot(self, slugs: frozenset[str] | None = None) -> MetricsSnapshot: ...
