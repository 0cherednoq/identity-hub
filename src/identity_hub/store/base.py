"""Порт хранилища (D-008).

Ядро работает с хранилищем только через `Store` и единицу работы `StoreTx`. PostgreSQL-реализация —
`identity_hub.postgres.store`, in-memory — `identity_hub.testing` (P10.1). Интерфейс растёт по мере
реализации задач: каждая задача добавляет абстрактные методы и их реализацию в PgStore.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from identity_hub.crypto.envelope import EncryptedPayload
from identity_hub.domain.regions import RegionState
from identity_hub.events import Event, EventDraft

NOTIFY_CHANNEL = "ih_events"
"""Канал NOTIFY внешних событий: будит диспетчер при коммите (FR-13.6)."""

__all__ = [
    "NOTIFY_CHANNEL",
    "CandidateQuery",
    "ChallengeRow",
    "ChargeOutcome",
    "DataRow",
    "IdentityRow",
    "LeaseCounters",
    "LeaseHistoryRow",
    "LeaseRow",
    "LeaseStatsRow",
    "LinkRow",
    "NewIdentity",
    "NewNotificationChannel",
    "NewNotificationRule",
    "NewProxy",
    "NewWebhookEndpoint",
    "NotificationAttempt",
    "NotificationChannelRow",
    "NotificationEnqueue",
    "NotificationRuleRow",
    "NotificationTargetRow",
    "ProxyRow",
    "QuotaCharge",
    "QuotaWindow",
    "SearchFilter",
    "ServiceRow",
    "SpaceRef",
    "Store",
    "StoreTx",
    "VerificationRow",
    "WebhookAttempt",
    "WebhookDeliveryRow",
    "WebhookEndpointRow",
]


@dataclass(frozen=True, slots=True)
class SpaceRef:
    id: UUID
    slug: str
    title: str


@dataclass(frozen=True, slots=True)
class ServiceRow:
    name: str
    title: str


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
    profile_secret: EncryptedPayload | None = None


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
    lifecycles: frozenset[str] = frozenset({"ready"})
    auth_states: frozenset[str] | None = None  # задано — вместо needs_auth (выборка для входа, FR-5.4)


@dataclass(frozen=True, slots=True)
class LeaseRow:
    """Живая аренда (ih_leases) или, в shared-режиме, только строка истории."""

    lease_id: UUID
    token: UUID
    identity_id: UUID
    fencing: int
    pool: str
    holder: str
    acquired_at: datetime
    expires_at: datetime
    idempotency_key: str | None = None
    proxy_id: UUID | None = None


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


@dataclass(frozen=True, slots=True)
class ChargeOutcome:
    """Результат списания: `duplicate` — ключ операции уже был (FR-7.5); `exhausted` — окно без единиц."""

    duplicate: bool = False
    exhausted: QuotaWindow | None = None
    reached: tuple[QuotaWindow, ...] = ()  # окна, израсходованные этой операцией до конца

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
    secret: EncryptedPayload


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
    secret: EncryptedPayload


@dataclass(frozen=True, slots=True)
class ChallengeRow:
    id: UUID
    identity_id: UUID
    kind: str
    hint: str
    context: EncryptedPayload
    answer_nonce: bytes | None
    answer_ciphertext: bytes | None
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
    evidence: EncryptedPayload | None


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
    secret: EncryptedPayload


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
    secret: EncryptedPayload


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


@dataclass(frozen=True, slots=True)
class WebhookAttempt:
    delivery_id: int
    attempt: int
    endpoint_id: UUID
    url: str
    secret: EncryptedPayload
    event: Event


@dataclass(frozen=True, slots=True)
class NewNotificationChannel:
    id: UUID
    title: str
    config: dict[str, Any]
    secret: EncryptedPayload


@dataclass(frozen=True, slots=True)
class NotificationChannelRow:
    id: UUID
    title: str
    config: dict[str, Any]
    enabled: bool
    created_at: datetime


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
    channel_secret: EncryptedPayload


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
    channel_secret: EncryptedPayload
    events: tuple[Event, ...]


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

    # --------------------------------------------------------- аккаунт и регионы (P2.3)

    @abstractmethod
    async def get_identity(self, identity_id: UUID) -> IdentityRow | None:
        """Аккаунт этого пространства (чужое пространство — None)."""

    @abstractmethod
    async def update_identity(
        self, identity_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        """Обновить колонки ih_identities; `expected` — compare-and-set (D-107). False — не совпало."""

    @abstractmethod
    async def load_region(self, identity_id: UUID, region: str, key: str) -> RegionState | None: ...

    @abstractmethod
    async def save_region(
        self, identity_id: UUID, region: str, key: str, previous: RegionState | None, new: RegionState
    ) -> bool:
        """Записать регион с проверкой `version` (previous=None — вставка). False — гонка."""

    # --------------------------------------------------------- регистрация (P2.4)

    @abstractmethod
    async def find_identity(
        self, identity_id: UUID, service: str, external_ref: str
    ) -> tuple[UUID, UUID] | None:
        """(id, space_id) аккаунта с этим id или (service, external_ref) — во всех пространствах (FR-2.2)."""

    @abstractmethod
    async def insert_identity(self, row: NewIdentity) -> bool:
        """False — конфликт уникальности (параллельная регистрация)."""

    @abstractmethod
    async def find_by_refs(self, refs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], tuple[UUID, UUID]]:
        """(service, external_ref) → (id, space_id) — во всех пространствах (импорт, FR-2.2)."""

    @abstractmethod
    async def existing_identities(self, ids: Sequence[UUID]) -> set[UUID]:
        """Какие из аккаунтов есть в этом пространстве."""

    @abstractmethod
    async def put_credentials(self, identity_id: UUID, payload: EncryptedPayload) -> None: ...

    @abstractmethod
    async def put_session(
        self,
        identity_id: UUID,
        slot: str,
        payload: EncryptedPayload,
        *,
        fencing: int,
        expires_at: datetime | None = None,
    ) -> bool:
        """Записать сессию слота; отклоняется, если записана более новой арендой (G-1.4)."""

    @abstractmethod
    async def insert_link(self, identity_id: UUID, role: str, linked_identity_id: UUID) -> bool:
        """False — связь с этой ролью уже есть (FR-4.1)."""

    # --------------------------------------------------------- данные аккаунта (P2.5)

    @abstractmethod
    async def get_profile_secret(self, identity_id: UUID) -> EncryptedPayload | None: ...

    @abstractmethod
    async def set_profile_secret(self, identity_id: UUID, payload: EncryptedPayload | None) -> None: ...

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
    async def link_counts(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int, list[str]]]:
        """id → (исходящих, входящих, роли исходящих) — одним запросом для страницы списка."""

    @abstractmethod
    async def dependents(self, identity_id: UUID, max_depth: int) -> list[tuple[UUID, int, list[str]]]:
        """Транзитивно зависящие аккаунты: (id, глубина, цепочка ролей) — анализ последствий."""

    # --------------------------------------------------------- аренда (P3.x)

    @abstractmethod
    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
        """Свободный допустимый аккаунт, заблокированный до конца транзакции (FOR UPDATE SKIP LOCKED).

        Порядок — наименее недавно арендованный (D-007). Истёкшие временные состояния availability
        считаются доступными (ленивое снятие, ТЗ §3.4).
        """

    @abstractmethod
    async def count_candidates(self, query: CandidateQuery, limit: int) -> int:
        """Сколько аккаунтов сейчас можно выдать под запрос (до `limit`) — ёмкость пула."""

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
    ) -> tuple[LeaseRow, LeaseRow | None]:
        """Захватить аренду заблокированного кандидата: fencing + 1, ih_leases, строка истории (G-4.2).

        Истёкшая аренда перезаписывается, её история закрывается как `expired` временем `expires_at`
        (FR-6.8). Возвращает (новая аренда, перезахваченная истёкшая или None).
        """

    @abstractmethod
    async def extend_lease(
        self, lease: LeaseRow, ttl_seconds: float, counters: LeaseCounters, *, shared: bool
    ) -> datetime | None:
        """Heartbeat: продлить живую аренду по token и сбросить счётчики. None — аренды больше нет."""

    @abstractmethod
    async def release_lease(
        self, lease: LeaseRow, reason: str, counters: LeaseCounters, *, shared: bool
    ) -> bool:
        """Освободить аренду по token (повтор и чужой token — no-op) и закрыть историю (G-4.2)."""

    @abstractmethod
    async def current_fencing(self, identity_id: UUID, *, lock: bool = False) -> int | None:
        """Текущий fencing_seq аккаунта; `lock=True` — FOR SHARE до конца транзакции."""

    @abstractmethod
    async def revoke_lease(self, identity_id: UUID) -> LeaseRow | None:
        """Отозвать живую аренду: fencing + 1, строка ih_leases освобождена, история закрыта как revoked."""

    # --------------------------------------------------------- квоты (P4.1)

    @abstractmethod
    async def charge_quotas(
        self, identity_id: UUID, charges: Sequence[QuotaCharge], *, operation_key: str | None, kind: str
    ) -> ChargeOutcome:
        """Атомарно списать по всем окнам (savepoint, G-2.2) и занять ключ операции (D-114)."""

    @abstractmethod
    async def bump_pacing(self, identity_id: UUID, charge: QuotaCharge) -> bool:
        """+1 к счётчику pacing; True — достигнут порог (счётчик обнулён), нужна пауза (D-113)."""

    @abstractmethod
    async def quota_usage(self, identity_id: UUID) -> list[QuotaWindow]:
        """Окна квот аккаунта, актуальные на now() (для карточки и проверки допустимости)."""

    @abstractmethod
    async def finish_operation_key(self, key: str, outcome: str) -> None: ...

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
        """(id, создан). Дубликат по fingerprint в пространстве возвращает существующий id (FR-8.1)."""

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
    async def choose_proxy(self, pool: str, max_identities: int | None, *, claim: bool) -> ProxyRow | None:
        """Наименее загруженный живой прокси пула с запасом ёмкости.

        `claim=True` — атомарно занять единицу ёмкости (`bound_count + 1`, D-117) для sticky-привязки.
        """

    @abstractmethod
    async def bind_proxy(self, identity_id: UUID, proxy_id: UUID, previous: UUID | None) -> None:
        """Привязать аккаунт к прокси; ёмкость прежнего прокси освобождается."""

    @abstractmethod
    async def adjust_proxy_load(self, proxy_id: UUID, delta: int) -> None:
        """bound_count += delta (не ниже 0) — привязка вне выбора прокси (импорт)."""

    @abstractmethod
    async def due_proxies(self, limit: int) -> list[UUID]:
        """Прокси в карантине, у которых наступил `next_check_at` (перепроверка по расписанию)."""

    # --------------------------------------------------------- верификации (P5.4)

    @abstractmethod
    async def load_verification(self, identity_id: UUID, factor: str) -> VerificationRow | None: ...

    @abstractmethod
    async def save_verification(
        self, identity_id: UUID, factor: str, values: dict[str, Any], evidence: EncryptedPayload | None = None
    ) -> None:
        """Upsert строки ih_verifications; `evidence` — перезаписать доказательства."""

    @abstractmethod
    async def due_regions(self, region: str, limit: int) -> list[tuple[UUID, str]]:
        """(identity_id, key) строк региона, у которых истёк `until` (снятие временных состояний)."""

    @abstractmethod
    async def stale_identities(
        self, service: str, region: str, key: str, older_than_seconds: float, limit: int
    ) -> list[UUID]:
        """Аккаунты сервиса, чей регион не сверялся дольше порога (или ни разу) — кандидаты reconcile."""

    # --------------------------------------------------------- поиск (P6.1)

    @abstractmethod
    async def search_identities(
        self,
        flt: SearchFilter,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None,
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
    async def lease_history(
        self, identity_id: UUID, *, limit: int, before: datetime | None
    ) -> list[LeaseHistoryRow]: ...

    @abstractmethod
    async def lease_stats(self, identity_id: UUID, since: datetime) -> LeaseStatsRow: ...

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
        context: EncryptedPayload,
        expires_at: datetime | None,
    ) -> bool:
        """False — у аккаунта уже открыт challenge (FR-5.5, уникальный частичный индекс)."""

    @abstractmethod
    async def get_challenge(self, challenge_id: UUID) -> ChallengeRow | None: ...

    @abstractmethod
    async def open_challenge_of(self, identity_id: UUID) -> ChallengeRow | None: ...

    @abstractmethod
    async def answer_challenge(self, challenge_id: UUID, nonce: bytes, ciphertext: bytes) -> bool:
        """Сохранить зашифрованный ответ; False — challenge уже закрыт."""

    @abstractmethod
    async def resolve_challenge(self, challenge_id: UUID) -> bool: ...

    @abstractmethod
    async def expired_challenges(self, limit: int) -> list[ChallengeRow]: ...

    # --------------------------------------------------------- секреты (P3.4)

    @abstractmethod
    async def get_credentials(self, identity_id: UUID) -> EncryptedPayload | None: ...

    @abstractmethod
    async def get_session(self, identity_id: UUID, slot: str) -> tuple[EncryptedPayload, str] | None:
        """(payload, status) сессии слота."""

    # --------------------------------------------------------- исходящие вебхуки (P8.3)

    @abstractmethod
    async def insert_webhook_endpoint(self, row: NewWebhookEndpoint) -> WebhookEndpointRow: ...

    @abstractmethod
    async def get_webhook_endpoint(self, endpoint_id: UUID) -> WebhookEndpointRow | None: ...

    @abstractmethod
    async def update_webhook_secret(self, endpoint_id: UUID, secret: EncryptedPayload) -> bool: ...

    @abstractmethod
    async def set_webhook_enabled(self, endpoint_id: UUID, enabled: bool, reason: str | None) -> bool: ...

    @abstractmethod
    async def retry_webhook_delivery(self, delivery_id: int) -> bool:
        """Вернуть доставку в pending и включить её endpoint (явное действие администратора)."""

    @abstractmethod
    async def webhook_deliveries(self, endpoint_id: UUID, limit: int) -> list[WebhookDeliveryRow]: ...

    # --------------------------------------------------------- Telegram-уведомления (P8.4)

    @abstractmethod
    async def insert_notification_channel(self, row: NewNotificationChannel) -> NotificationChannelRow: ...

    @abstractmethod
    async def notification_channels(self) -> list[NotificationChannelRow]: ...

    @abstractmethod
    async def update_notification_channel(
        self, channel_id: UUID, values: dict[str, Any], secret: EncryptedPayload | None
    ) -> bool: ...

    @abstractmethod
    async def delete_notification_channel(self, channel_id: UUID) -> bool: ...

    @abstractmethod
    async def insert_notification_rule(self, row: NewNotificationRule) -> NotificationRuleRow: ...

    @abstractmethod
    async def notification_rules(self) -> list[NotificationRuleRow]: ...

    @abstractmethod
    async def update_notification_rule(self, rule_id: UUID, values: dict[str, Any]) -> bool: ...

    @abstractmethod
    async def delete_notification_rule(self, rule_id: UUID) -> bool: ...


class Store(ABC):
    @abstractmethod
    def transaction(
        self, space: SpaceRef, *, session: Any | None = None
    ) -> AbstractAsyncContextManager[StoreTx]:
        """Своя транзакция библиотеки, либо savepoint в сессии пользователя (ТЗ T-1, T-3)."""

    @abstractmethod
    async def sync_services(self, services: Sequence[ServiceRow]) -> None:
        """Зеркало реестра сервисов для админки (FR-1.3)."""

    @abstractmethod
    async def ensure_space(
        self, slug: str, title: str | None = None, space_id: UUID | None = None
    ) -> SpaceRef:
        """Получить или создать пространство."""

    @abstractmethod
    async def get_space(self, slug: str) -> SpaceRef | None: ...

    # --------------------------------------------------------- журнал событий (P8.1)

    @abstractmethod
    async def process_events(
        self,
        consumer: str,
        handler: Callable[[list[Event]], Awaitable[None]],
        *,
        limit: int,
    ) -> int | None:
        """Обработать пачку внешних событий потребителя (G-5.2, G-5.5).

        Строка курсора блокируется `FOR UPDATE SKIP LOCKED` на время пачки (None — занята другим
        экземпляром). Читаются события по `(tx_id, id)` строго после курсора и ниже горизонта
        `pg_snapshot_xmin`. Курсор продвигается только если `handler` завершился без ошибки.
        """

    @abstractmethod
    def listen(self, channel: str) -> AbstractAsyncContextManager[asyncio.Event | None]:
        """Подписка на NOTIFY: событие взводится при уведомлении; None — LISTEN недоступен (опрос)."""

    # --------------------------------------------------------- исходящие вебхуки (P8.3)

    @abstractmethod
    async def enabled_webhook_endpoints(self) -> list[WebhookEndpointRow]: ...

    @abstractmethod
    async def identity_labels(self, identity_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]: ...

    @abstractmethod
    async def enqueue_webhook_deliveries(self, pairs: Sequence[tuple[UUID, UUID]]) -> int: ...

    @abstractmethod
    async def claim_webhook_deliveries(self, *, limit: int, claim_for: timedelta) -> list[WebhookAttempt]:
        """Коротко зарезервировать due-доставки; сетевой I/O выполняется после коммита."""

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
    ) -> bool: ...
