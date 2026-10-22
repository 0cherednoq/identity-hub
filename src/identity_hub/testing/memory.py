"""Детерминированная in-memory реализация порта хранилища.

Она повторяет наблюдаемую семантику PostgreSQL-адаптера, но сериализует транзакции одним
``asyncio.Lock``. Перед транзакцией снимается глубокая копия состояния: исключение возвращает
снимок, поэтому тесты получают настоящую атомарность и rollback без SQL.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pydantic_core

from identity_hub._ids import new_id
from identity_hub.clock import Clock, SystemClock
from identity_hub.crypto.envelope import (
    DecryptionFailed,
    EncryptedPayload,
    PlaintextPayload,
    StoredPayload,
    associated_data,
    credential_associated_data,
)
from identity_hub.domain.idempotency import FINGERPRINT_VERSION, LEGACY_FINGERPRINT_VERSION
from identity_hub.domain.provider_operations import CLAIMABLE as CLAIMABLE_OPERATIONS
from identity_hub.domain.provider_operations import TERMINAL as TERMINAL_OPERATIONS
from identity_hub.domain.provider_operations import resolve_outcome, resolve_recovery
from identity_hub.domain.query import profile_sort_text
from identity_hub.domain.region_specs import AVAILABILITY, STANDING
from identity_hub.domain.regions import RegionState
from identity_hub.enums import (
    AuthState,
    Availability,
    LeaseEndReason,
    LeaseUnavailableReason,
    Lifecycle,
    SessionStatus,
    Severity,
)
from identity_hub.errors import (
    AlreadyExists,
    ConsumerCursorExpired,
    IdempotencyConflict,
    LeaseOwnershipLost,
    LeaseUnavailable,
    NotFound,
    RevisionConflict,
    SpaceMismatch,
    ValidationFailed,
)
from identity_hub.events import Event, EventDraft
from identity_hub.rollups import BucketSize, RollupDelta, RollupKey, RollupRow
from identity_hub.snapshots import StateSnapshotFrame, StateSnapshotRow
from identity_hub.store.base import (
    NO_LEASE_OPTIONS,
    PICK_DEMOTE_CHUNK,
    UNREADABLE_IMPORT_ITEM,
    AcceptanceRow,
    AttentionChallengeRow,
    AttentionIdentityRow,
    CandidateQuery,
    ChallengeRow,
    ChargeOutcome,
    CredentialMetadataRow,
    CredentialRow,
    DataRow,
    DeliveryEnqueue,
    DoctorIssue,
    EventReadFilter,
    EventReadRow,
    IdempotencyClaim,
    IdempotencyRequest,
    IdentityRow,
    ImportItemResult,
    ImportItemRow,
    ImportRunRow,
    LeaseCounters,
    LeaseFence,
    LeaseHistoryRow,
    LeaseOptions,
    LeaseReadRow,
    LeaseRenewal,
    LeaseRow,
    LeaseStatsRow,
    LinkRow,
    MetricsSnapshot,
    NewIdentity,
    NewImportItem,
    NewImportRun,
    NewNotificationChannel,
    NewNotificationRule,
    NewProxy,
    NewWebhookEndpoint,
    NotificationAttempt,
    NotificationChannelRow,
    NotificationChannelSecretRow,
    NotificationDeliveryReadRow,
    NotificationEnqueue,
    NotificationRuleRow,
    NotificationTargetRow,
    OverviewRequirementRow,
    OverviewServiceRow,
    OverviewSnapshot,
    ProviderClaim,
    ProviderOperationRow,
    ProxyBindingHistoryRow,
    ProxyBoundIdentityRow,
    ProxyPoolCapacityRow,
    ProxyReadRow,
    ProxyRow,
    QuotaCharge,
    QuotaWindow,
    RotationBatch,
    SearchFilter,
    SecretConversionBatch,
    ServiceRow,
    SessionHistoryRow,
    SessionMetadataRow,
    SessionWrite,
    SpaceRef,
    Store,
    StoreTx,
    UnreadableSecret,
    VerificationRow,
    WebhookAttempt,
    WebhookDeliveryRow,
    WebhookEndpointReadRow,
    WebhookEndpointRow,
    YieldRequest,
    credential_label,
    expiry_reason,
)

__all__ = ["MemoryStore", "MemoryTx"]


MEMORY_SCHEMA_REVISION = "memory"


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    space_id: UUID
    owner_identity_id: UUID | None
    operation: str
    fingerprint_version: int
    fingerprint: str | None
    attempt: int
    created_at: datetime
    expires_at: datetime
    outcome: str | None = None
    result_type: str | None = None
    result_version: int | None = None
    result: dict[str, Any] | None = None
    fingerprint_key_id: str | None = None  # команда с секретами (D-208)


@dataclass(frozen=True)
class _SessionRecord:
    """Запись сессии слота (D-218): версии и время как в PostgreSQL (R2-P08)."""

    id: UUID
    identity_id: UUID
    slot: str
    payload: StoredPayload
    status: str
    written_by_fencing: int
    expires_at: datetime | None
    version: int
    revision: int
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None
    value_fingerprint: str | None = None
    last_verified_at: datetime | None = None
    last_rejected_at: datetime | None = None
    reject_code: str | None = None


@dataclass
class _MemoryState:
    spaces: dict[str, SpaceRef] = field(default_factory=dict)
    services: dict[str, ServiceRow] = field(default_factory=dict)
    identities: dict[UUID, IdentityRow] = field(default_factory=dict)
    regions: dict[tuple[UUID, str, str], RegionState] = field(default_factory=dict)
    # (identity, kind, name) → (секрет, метаданные): несколько credentials на аккаунт (D-214)
    credentials: dict[tuple[UUID, str, str], tuple[StoredPayload, CredentialRow]] = field(
        default_factory=dict
    )
    profile_secrets: dict[UUID, StoredPayload] = field(default_factory=dict)
    # все записи сессий по id, включая отозванные (история, D-218); текущая запись слота — в session_slots
    sessions: dict[UUID, _SessionRecord] = field(default_factory=dict)
    session_slots: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    links: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    data: dict[tuple[UUID, str], DataRow] = field(default_factory=dict)
    leases: dict[UUID, LeaseRow] = field(default_factory=dict)
    history: dict[UUID, LeaseHistoryRow] = field(default_factory=dict)
    # lease_id → пространство: история переживает удаление аккаунта, как ih_lease_history.space_id
    history_spaces: dict[UUID, UUID] = field(default_factory=dict)
    # token аренды по lease_id: в строке истории его нет, а fence разделяемой аренды сверяет его (D-212)
    history_tokens: dict[UUID, UUID] = field(default_factory=dict)
    throttle_state: dict[tuple[UUID, str], datetime] = field(default_factory=dict)
    # identity → (время, порядковый номер) последней попытки фоновой сверки
    probe_attempts: dict[UUID, tuple[datetime, int]] = field(default_factory=dict)
    next_probe_attempt: int = 1
    quotas: dict[tuple[UUID, str, int, datetime], int] = field(default_factory=dict)
    operation_keys: dict[tuple[str, str], _IdempotencyRecord] = field(default_factory=dict)
    import_runs: dict[UUID, ImportRunRow] = field(default_factory=dict)
    import_items: dict[tuple[UUID, int], dict[str, Any]] = field(default_factory=dict)
    acceptance: dict[UUID, AcceptanceRow] = field(default_factory=dict)
    provider_operations: dict[UUID, ProviderOperationRow] = field(default_factory=dict)
    proxies: dict[UUID, ProxyRow] = field(default_factory=dict)
    proxy_load: dict[UUID, int] = field(default_factory=dict)
    bindings: dict[UUID, UUID] = field(default_factory=dict)
    binding_times: dict[UUID, datetime] = field(default_factory=dict)
    link_times: dict[tuple[UUID, str], datetime] = field(default_factory=dict)
    verifications: dict[tuple[UUID, str], VerificationRow] = field(default_factory=dict)
    challenges: dict[UUID, ChallengeRow] = field(default_factory=dict)
    lease_logs: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[Event, UUID, bool]] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    rollups: dict[RollupKey, float] = field(default_factory=dict)
    metric_batches: dict[UUID, datetime] = field(default_factory=dict)  # квитанция → время записи
    state_snapshots: dict[tuple[UUID, str, str, str, datetime], int] = field(default_factory=dict)
    snapshot_coverage: dict[tuple[UUID, datetime], tuple[datetime, str, bool]] = field(default_factory=dict)
    webhook_endpoints: dict[UUID, WebhookEndpointRow] = field(default_factory=dict)
    webhook_deliveries: dict[int, WebhookDeliveryRow] = field(default_factory=dict)
    notification_channels: dict[UUID, tuple[NotificationChannelRow, StoredPayload, UUID]] = field(
        default_factory=dict
    )
    notification_rules: dict[UUID, NotificationRuleRow] = field(default_factory=dict)
    notification_rule_spaces: dict[UUID, UUID] = field(default_factory=dict)
    # канал → конец паузы после 429 (ih_notification_channels.paused_until)
    channel_pauses: dict[UUID, datetime] = field(default_factory=dict)
    notifications: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_event_id: int = 1
    # порядок захватов: при одинаковом времени FakeClock LRU всё равно различает давность (D-209)
    lease_order: dict[UUID, int] = field(default_factory=dict)
    next_lease_order: int = 1
    next_webhook_delivery_id: int = 1
    next_notification_id: int = 1


# Снимок для отката транзакции (R2-P22). Значения разделов — неизменяемые dataclass и кортежи,
# поэтому словарям хватает поверхностной копии. Исключения: элементы `import_items` и
# `notifications` — изменяемые словари, их копия глубокая; журналы `events` и `lease_logs`
# транзакция только дополняет, поэтому запоминается их длина. Новый раздел состояния с
# изменяемыми значениями нужно добавить в `_DEEP_SECTIONS`.
_DEEP_SECTIONS = frozenset({"import_items", "notifications"})
_APPEND_ONLY_SECTIONS = frozenset({"events", "lease_logs"})


def _snapshot(state: _MemoryState) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for item in fields(state):
        value = getattr(state, item.name)
        if item.name in _APPEND_ONLY_SECTIONS:
            snapshot[item.name] = len(value)
        elif item.name in _DEEP_SECTIONS:
            snapshot[item.name] = deepcopy(value)
        elif isinstance(value, dict):
            snapshot[item.name] = dict(value)
        else:  # счётчики: неизменяемые значения
            snapshot[item.name] = value
    return snapshot


def _restore(state: _MemoryState, snapshot: dict[str, Any]) -> None:
    for name, value in snapshot.items():
        if name in _APPEND_ONLY_SECTIONS:
            del getattr(state, name)[value:]
        else:
            setattr(state, name, value)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _memory_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


def _window_start(now: datetime, charge: QuotaCharge) -> datetime:
    seconds = int((now - _utc(charge.origin)).total_seconds())
    return _utc(charge.origin) + timedelta(seconds=(seconds // charge.window_seconds) * charge.window_seconds)


def _gone(state: Any, token: UUID) -> LeaseRenewal:
    """Чем кончилась аренда с этим token — как `_renewals_of_gone` в PostgreSQL."""
    lease_id = next((key for key, value in state.history_tokens.items() if value == token), None)
    history = state.history.get(lease_id) if lease_id is not None else None
    if history is None:
        return LeaseRenewal(None)
    if history.released_at is not None:
        return LeaseRenewal(None, LeaseEndReason(history.end_reason))
    current = state.leases.get(history.identity_id)
    if current is None or current.token != token:
        return LeaseRenewal(None)
    # строка наша, но срок вышел
    return LeaseRenewal(None, expiry_reason(history.hold_deadline, current.expires_at))


def _merge_counters(row: LeaseHistoryRow, counters: LeaseCounters) -> dict[str, Any]:
    operations = dict(row.operations)
    for kind, count in counters.operations.items():
        operations[kind] = operations.get(kind, 0) + count
    return {
        "operations": operations,
        "succeeded": row.succeeded + counters.succeeded,
        "failed": row.failed + counters.failed,
        "last_error": counters.last_error if counters.last_error is not None else row.last_error,
    }


def _pause_deliveries(state: Any, endpoint_id: UUID) -> None:
    """Очередь отключённого endpoint'а ждёт его включения в статусе `paused`."""
    for delivery_id, row in list(state.webhook_deliveries.items()):
        if row.endpoint_id == endpoint_id and row.status in {"pending", "failed"}:
            state.webhook_deliveries[delivery_id] = replace(row, status="paused")


def _resume_deliveries(state: Any, endpoint_id: UUID, now: datetime) -> None:
    for delivery_id, row in list(state.webhook_deliveries.items()):
        if row.endpoint_id == endpoint_id and row.status == "paused":
            state.webhook_deliveries[delivery_id] = replace(row, status="pending", next_attempt_at=now)


def _with_among_ids(query: CandidateQuery) -> CandidateQuery:
    """`among` как множество id; подзапрос SQL в памяти выполнить нельзя (R2-P02)."""
    if query.among is None or isinstance(query.among, frozenset):
        return query
    if hasattr(query.among, "compile") or isinstance(query.among, (str, bytes)):
        raise ValidationFailed(
            "among: подзапрос SQL работает только с PostgreSQL; в MemoryStore передайте "
            "коллекцию id или функцию-предикат от Identity"
        )
    try:
        return replace(query, among=frozenset(query.among))
    except TypeError:
        raise ValidationFailed("among: ожидается коллекция id, подзапрос или функция-предикат") from None


class MemoryTx(StoreTx):
    """Транзакция MemoryStore. Экземпляр живёт только внутри context manager."""

    def __init__(self, store: MemoryStore, space: SpaceRef, now: datetime) -> None:
        self.store = store
        self.space = space
        self._now = now
        self._notify = False
        self._revised: set[UUID] = set()  # identity, чья ревизия уже поднята этой командой (D-205)

    @property
    def state(self) -> _MemoryState:
        return self.store._state

    async def now(self) -> datetime:
        return self._now

    @asynccontextmanager
    async def savepoint(self) -> AsyncIterator[None]:
        snapshot = _snapshot(self.state)
        revised, notify = set(self._revised), self._notify
        try:
            yield
        except BaseException:
            _restore(self.state, snapshot)
            self._revised, self._notify = revised, notify
            raise

    def _own(self, identity_id: UUID) -> IdentityRow | None:
        row = self.state.identities.get(identity_id)
        return row if row is not None and row.space_id == self.space.id else None

    async def emit(self, drafts: Sequence[EventDraft]) -> list[UUID]:
        ids: list[UUID] = []
        for draft in drafts:
            sequence: int | None = None
            revision: int | None = None
            if draft.identity_id is not None:
                identity = self._own(draft.identity_id)
                if identity is not None:
                    bump = draft.spec.aggregate and identity.id not in self._revised
                    sequence = identity.event_seq + 1
                    revision = identity.revision + int(bump)
                    self.state.identities[identity.id] = replace(
                        identity, event_seq=sequence, revision=revision, updated_at=self._now
                    )
                    if bump:
                        self._revised.add(identity.id)
            event_id = new_id()  # UUIDv7, как в PostgreSQL: порядок при равном времени (R2-P10)
            event = Event(
                id=self.state.next_event_id,
                event_id=event_id,
                type=draft.type,
                severity=draft.effective_severity,
                occurred_at=self._now,
                space=self.space.slug,
                service=draft.service,
                identity_id=draft.identity_id,
                proxy_id=draft.proxy_id,
                lease_id=draft.lease_id,
                sequence=sequence,
                actor=draft.actor,
                data=dict(draft.data),
                revision=revision,
            )
            self.state.next_event_id += 1
            self.state.events.append((event, self.space.id, draft.spec.external))
            self._notify |= draft.spec.external
            ids.append(event_id)
        return ids

    async def get_identities(self, identity_ids: Sequence[UUID]) -> list[IdentityRow]:
        rows = (self._own(identity_id) for identity_id in dict.fromkeys(identity_ids))
        return [row for row in rows if row is not None]

    async def get_identity(self, identity_id: UUID) -> IdentityRow | None:
        return self._own(identity_id)

    async def lock_identity_revision(self, identity_id: UUID, expected_revision: int) -> int:
        row = self._own(identity_id)
        if row is None:
            raise NotFound("identity", identity_id)
        if row.revision != expected_revision:
            raise RevisionConflict("identity", identity_id, expected_revision, row.revision)
        return row.revision

    async def update_identity(
        self, identity_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        row = self._own(identity_id)
        if row is None or any(getattr(row, key) != value for key, value in (expected or {}).items()):
            return False
        self.state.identities[identity_id] = replace(row, **values, updated_at=self._now)
        return True

    async def load_region(self, identity_id: UUID, region: str, key: str) -> RegionState | None:
        return self.state.regions.get((identity_id, region, key)) if self._own(identity_id) else None

    async def load_regions(
        self, identity_ids: Sequence[UUID], region: str, key: str
    ) -> dict[UUID, RegionState]:
        wanted = set(identity_ids)
        return {
            identity_id: state
            for (identity_id, found_region, found_key), state in self.state.regions.items()
            if identity_id in wanted
            and self._own(identity_id) is not None
            and found_region == region
            and found_key == key
        }

    async def save_region(
        self, identity_id: UUID, region: str, key: str, previous: RegionState | None, new: RegionState
    ) -> bool:
        if self._own(identity_id) is None:
            return False
        compound = (identity_id, region, key)
        current = self.state.regions.get(compound)
        if previous is None:
            if current is not None:
                return False
        elif current is None or current.version != previous.version:
            return False
        self.state.regions[compound] = new
        return True

    async def find_identity(
        self, identity_id: UUID, service: str, normalized_ref: str
    ) -> tuple[UUID, UUID] | None:
        for row in self.state.identities.values():
            if row.id == identity_id or (row.service == service and row.normalized_ref == normalized_ref):
                return row.id, row.space_id
        return None

    async def insert_identity(self, row: NewIdentity) -> bool:
        if await self.find_identity(row.id, row.service, row.unique_ref) is not None:
            return False
        identity = IdentityRow(
            id=row.id,
            space_id=self.space.id,
            service=row.service,
            external_ref=row.external_ref,
            lifecycle=row.lifecycle,
            standing=row.standing,
            standing_code=None,
            standing_until=None,
            auth_state=row.auth_state,
            availability="available",
            availability_until=None,
            requirements=[],
            tags=list(row.tags),
            attributes=deepcopy(row.attributes),
            profile=deepcopy(row.profile),
            labels=deepcopy(row.labels),
            note=row.note,
            search_text=row.search_text,
            verified_factors=[],
            error_score=0.0,
            fencing_seq=0,
            event_seq=0,
            created_at=self._now,
            updated_at=self._now,
            normalized_ref=row.unique_ref,
            normalize_version=row.normalize_version,
        )
        self.state.identities[row.id] = identity
        if row.profile_secret is not None:
            self.state.profile_secrets[row.id] = row.profile_secret
        return True

    async def find_by_refs(self, refs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], tuple[UUID, UUID]]:
        wanted = set(refs)
        return {
            (row.service, row.normalized_ref): (row.id, row.space_id)
            for row in self.state.identities.values()
            if (row.service, row.normalized_ref) in wanted
        }

    async def existing_identities(self, ids: Sequence[UUID]) -> set[UUID]:
        return {identity_id for identity_id in ids if self._own(identity_id) is not None}

    async def put_credentials(
        self,
        identity_id: UUID,
        payload: StoredPayload,
        *,
        value_fingerprint: str | None = None,
    ) -> None:
        if self._own(identity_id):
            await self.put_credential(
                identity_id,
                "default",
                "default",
                payload,
                value_fingerprint=value_fingerprint,
            )

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
        if self._own(identity_id) is None:
            raise NotFound("identity", identity_id)
        key = (identity_id, kind, name)
        old = self.state.credentials.get(key)
        if old is None:
            row = CredentialRow(
                new_id(),
                identity_id,
                kind,
                name,
                deepcopy(public_value),
                "active",
                expires_at,
                1,
                1,
                self._now,
                self._now,
                value_fingerprint,
            )
        else:
            row = replace(
                old[1],
                public_value=deepcopy(public_value),
                expires_at=expires_at,
                revision=old[1].revision + 1,
                version=old[1].version + 1,
                updated_at=self._now,
                value_fingerprint=value_fingerprint,
                last_verified_at=None,
                last_rejected_at=None,
                reject_code=None,
            )
        self.state.credentials[key] = (deepcopy(payload), row)
        return row

    async def get_credential(
        self, identity_id: UUID, kind: str, name: str
    ) -> tuple[StoredPayload, CredentialRow] | None:
        if self._own(identity_id) is None:
            return None
        found = self.state.credentials.get((identity_id, kind, name))
        return (found[0], found[1]) if found is not None else None

    async def set_credential_lifecycle(
        self, identity_id: UUID, kind: str, name: str, lifecycle: str
    ) -> CredentialRow | None:
        found = await self.get_credential(identity_id, kind, name)
        if found is None:
            return None
        row = replace(found[1], lifecycle=lifecycle, revision=found[1].revision + 1, updated_at=self._now)
        self.state.credentials[(identity_id, kind, name)] = (found[0], row)
        return row

    async def list_credentials(self, identity_id: UUID) -> list[CredentialRow]:
        if self._own(identity_id) is None:
            return []
        return [
            row for (owner, _, _), (_, row) in sorted(self.state.credentials.items()) if owner == identity_id
        ]

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
        found = await self.get_credential(identity_id, kind, name)
        if found is None:
            return None
        row = replace(
            found[1],
            lifecycle=("disabled" if disable and found[1].lifecycle != "revoked" else found[1].lifecycle),
            last_verified_at=None if rejected else self._now,
            last_rejected_at=self._now if rejected else None,
            reject_code=code if rejected else None,
            revision=found[1].revision + 1,
            updated_at=self._now,
        )
        self.state.credentials[(identity_id, kind, name)] = (found[0], row)
        return row

    async def credential_candidate_state(self, identity_id: UUID) -> bool | None:
        rows = await self.list_credentials(identity_id)
        if not rows:
            return None
        return any(
            row.lifecycle == "active"
            and (row.expires_at is None or row.expires_at > self._now)
            and row.last_rejected_at is None
            for row in rows
        )

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
        if self._own(identity_id) is None:
            return None
        current = self._current_session(identity_id, slot)
        if current is not None and current.written_by_fencing > fencing:
            return None
        if current is not None and mode == "in_place":
            updated = replace(
                current,
                payload=deepcopy(payload),
                status=SessionStatus.ACTIVE,
                written_by_fencing=fencing,
                expires_at=expires_at,
                version=current.version + 1,
                revision=current.revision + 1,
                updated_at=self._now,
                value_fingerprint=value_fingerprint,
                last_verified_at=None,
                last_rejected_at=None,
                reject_code=None,
            )
            self.state.sessions[current.id] = updated
            return SessionWrite(current.id, updated.revision)
        if current is not None:
            self.state.sessions[current.id] = replace(
                current, status=SessionStatus.REVOKED, revoked_at=self._now, updated_at=self._now
            )
        record = _SessionRecord(
            new_id(),
            identity_id,
            slot,
            deepcopy(payload),
            SessionStatus.ACTIVE,
            fencing,
            expires_at,
            1,
            current.revision + 1 if current is not None else 1,
            self._now,
            self._now,
            None,
            value_fingerprint,
        )
        self.state.sessions[record.id] = record
        self.state.session_slots[(identity_id, slot)] = record.id
        return SessionWrite(record.id, record.revision, current.id if current is not None else None)

    def _current_session(self, identity_id: UUID, slot: str) -> _SessionRecord | None:
        session_id = self.state.session_slots.get((identity_id, slot))
        record = self.state.sessions.get(session_id) if session_id is not None else None
        return record if record is not None and record.status != SessionStatus.REVOKED else None

    async def revoke_session(self, identity_id: UUID, slot: str) -> bool:
        current = self._current_session(identity_id, slot) if self._own(identity_id) else None
        if current is None:
            return False
        self.state.sessions[current.id] = replace(
            current, status=SessionStatus.REVOKED, revoked_at=self._now, updated_at=self._now
        )
        return True

    async def session_history(self, identity_id: UUID, slot: str) -> list[SessionHistoryRow]:
        if self._own(identity_id) is None:
            return []
        records = sorted(
            (r for r in self.state.sessions.values() if r.identity_id == identity_id and r.slot == slot),
            key=lambda r: (r.revision, r.created_at),
        )
        return [
            SessionHistoryRow(
                r.id,
                r.status,
                r.revision,
                r.created_at,
                r.updated_at,
                r.revoked_at,
                r.value_fingerprint,
                r.last_verified_at,
                r.last_rejected_at,
                r.reject_code,
            )
            for r in records
        ]

    async def mark_session_validity(
        self,
        identity_id: UUID,
        slot: str,
        *,
        rejected: bool,
        code: str | None = None,
    ) -> bool:
        current = self._current_session(identity_id, slot) if self._own(identity_id) else None
        if current is None:
            return False
        self.state.sessions[current.id] = replace(
            current,
            last_verified_at=None if rejected else self._now,
            last_rejected_at=self._now if rejected else None,
            reject_code=code if rejected else None,
            revision=current.revision + 1,
            updated_at=self._now,
        )
        return True

    async def insert_link(self, identity_id: UUID, role: str, linked_identity_id: UUID) -> bool:
        key = (identity_id, role)
        if key in self.state.links or self._own(identity_id) is None or self._own(linked_identity_id) is None:
            return False
        self.state.links[key] = linked_identity_id
        self.state.link_times[key] = self._now
        return True

    async def get_profile_secret(self, identity_id: UUID) -> StoredPayload | None:
        return self.state.profile_secrets.get(identity_id) if self._own(identity_id) else None

    async def set_profile_secret(self, identity_id: UUID, payload: StoredPayload | None) -> None:
        if self._own(identity_id) is None:
            return
        if payload is None:
            self.state.profile_secrets.pop(identity_id, None)
        else:
            self.state.profile_secrets[identity_id] = payload

    async def incoming_links(self, identity_id: UUID) -> list[tuple[UUID, str]]:
        return [
            (source, role) for (source, role), target in self.state.links.items() if target == identity_id
        ]

    async def delete_identity(self, identity_id: UUID) -> bool:
        if self._own(identity_id) is None:
            return False
        del self.state.identities[identity_id]
        self.state.acceptance.pop(identity_id, None)
        self.state.probe_attempts.pop(identity_id, None)
        # как каскад в PostgreSQL: строка аренды уходит, а её незакрытая история закрывается (R2-D14)
        stale = self.state.leases.pop(identity_id, None)
        if stale is not None:
            history = self.state.history.get(stale.lease_id)
            if history is not None and history.released_at is None:
                self.state.history[stale.lease_id] = replace(
                    history,
                    released_at=stale.expires_at,
                    duration=stale.expires_at - history.acquired_at,
                    end_reason=str(expiry_reason(history.hold_deadline, stale.expires_at)),
                )
        for credential_key in [key for key in self.state.credentials if key[0] == identity_id]:
            del self.state.credentials[credential_key]
        self.state.profile_secrets.pop(identity_id, None)
        bound = self.state.bindings.pop(identity_id, None)
        self.state.binding_times.pop(identity_id, None)
        if bound is not None:  # место на прокси освобождается, как каскад в PostgreSQL (R2-P06)
            self.state.proxy_load[bound] = max(0, self.state.proxy_load.get(bound, 0) - 1)
        for region_key in [item for item in self.state.regions if item[0] == identity_id]:
            del self.state.regions[region_key]
        for session_id in [
            key for key, record in self.state.sessions.items() if record.identity_id == identity_id
        ]:
            del self.state.sessions[session_id]
        for slot_key in [key for key in self.state.session_slots if key[0] == identity_id]:
            del self.state.session_slots[slot_key]
        for data_key in [item for item in self.state.data if item[0] == identity_id]:
            del self.state.data[data_key]
        for throttle_key in [item for item in self.state.throttle_state if item[0] == identity_id]:
            del self.state.throttle_state[throttle_key]
        for verification_key in [item for item in self.state.verifications if item[0] == identity_id]:
            del self.state.verifications[verification_key]
        for verification_key, verification in list(self.state.verifications.items()):
            if verification.via_identity_id == identity_id:  # как FK ... ON DELETE SET NULL
                self.state.verifications[verification_key] = replace(verification, via_identity_id=None)
        for key, target in list(self.state.links.items()):
            if key[0] == identity_id or target == identity_id:
                del self.state.links[key]
                self.state.link_times.pop(key, None)
        return True

    async def load_data(self, identity_id: UUID, namespace: str) -> DataRow | None:
        return self.state.data.get((identity_id, namespace)) if self._own(identity_id) else None

    async def save_data(
        self,
        identity_id: UUID,
        namespace: str,
        data: dict[str, Any],
        schema_version: int,
        expected_version: int | None,
    ) -> int | None:
        key = (identity_id, namespace)
        current = self.state.data.get(key)
        if self._own(identity_id) is None:
            return None
        if expected_version is None:
            if current is not None:
                return None
            version = 1
        else:
            if current is None or current.version != expected_version:
                return None
            version = expected_version + 1
        self.state.data[key] = DataRow(deepcopy(data), schema_version, version, self._now)
        return version

    async def delete_link(self, identity_id: UUID, role: str) -> UUID | None:
        if self._own(identity_id) is None:
            return None
        self.state.link_times.pop((identity_id, role), None)
        return self.state.links.pop((identity_id, role), None)

    def _link(self, source: UUID, role: str, target: UUID, outgoing: bool) -> LinkRow:
        other = self.state.identities[target if outgoing else source]
        created = self.state.link_times.get((source, role), other.created_at)  # время связи (R2-P14)
        return LinkRow(source, role, target, other.service, other.external_ref, created)

    async def links_of(self, identity_id: UUID) -> tuple[list[LinkRow], list[LinkRow]]:
        # порядок как в PostgreSQL: исходящие по роли, входящие по роли и id
        links = sorted(self.state.links.items(), key=lambda item: (item[0][1], item[0][0]))
        outgoing = [self._link(s, r, t, True) for (s, r), t in links if s == identity_id]
        incoming = [self._link(s, r, t, False) for (s, r), t in links if t == identity_id]
        return outgoing, incoming

    async def link_counts(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int, list[str]]]:
        result: dict[UUID, tuple[int, int, list[str]]] = {}
        for identity_id in ids:
            outgoing = [role for (source, role), _ in self.state.links.items() if source == identity_id]
            incoming = sum(target == identity_id for target in self.state.links.values())
            result[identity_id] = len(outgoing), incoming, sorted(outgoing)
        return result

    async def dependents(self, identity_id: UUID, max_depth: int) -> list[tuple[UUID, int, list[str]]]:
        found: dict[UUID, tuple[int, list[str]]] = {}
        frontier: list[tuple[UUID, list[str]]] = [(identity_id, [])]
        for depth in range(1, max_depth + 1):
            upcoming: list[tuple[UUID, list[str]]] = []
            for target, path in frontier:
                for (source, role), linked in self.state.links.items():
                    if linked == target and source not in found and source != identity_id:
                        # путь от исходного аккаунта наружу, как `path || role` в PostgreSQL (R2-P14)
                        found[source] = depth, [*path, role]
                        upcoming.append((source, [*path, role]))
            frontier = upcoming
        return [(key, depth, path) for key, (depth, path) in found.items()]

    def _candidate(self, row: IdentityRow, query: CandidateQuery) -> bool:
        now = self._now
        if (
            row.service != query.service
            or row.lifecycle not in query.lifecycles
            or row.standing not in query.standing
        ):
            return False
        if query.auth_states is not None:
            if row.auth_state not in query.auth_states:
                return False
        elif query.needs_auth and row.auth_state != "authenticated":
            return False
        # истёкший `until` лениво снимается только у paused/cooling; карантин снимает команда (R2-P11)
        if row.availability != "available" and not (
            row.availability in AVAILABILITY.on_expire
            and row.availability_until
            and row.availability_until <= now
        ):
            return False
        if not query.required_factors.issubset(row.verified_factors) or not query.tags.issubset(row.tags):
            return False
        # фактор с вышедшим сроком действия допуска не даёт и до прогона обслуживания (R2-F13)
        if any(
            (state := self.state.regions.get((row.id, "verification", factor))) is not None
            and state.state == "verified"
            and state.until is not None
            and state.until <= now
            for factor in query.required_factors
        ):
            return False
        # как JSONB `@>`: ключ должен быть, `{"country": None}` не совпадает с отсутствующим полем (R2-P12)
        if query.labels and any(k not in row.labels or row.labels[k] != v for k, v in query.labels.items()):
            return False
        if query.profile and not self._contains(row.profile, query.profile):
            return False
        if query.exclude and row.id in query.exclude:
            return False
        if query.without_link and any(
            source == row.id and role in query.without_link for source, role in self.state.links
        ):
            return False
        if query.not_target_of is not None and self._is_target_of(row.id, query.not_target_of):
            return False
        if query.is_target_of is not None and not self._is_target_of(row.id, query.is_target_of):
            return False
        if query.among is not None and row.id not in query.among:  # множество из `_with_among_ids`
            return False
        lease = self.state.leases.get(row.id)
        if not query.shared and lease is not None and lease.expires_at > now:
            return False
        if (
            not query.shared
            and row.yield_reserved_until is not None
            and row.yield_reserved_until > now
            and row.yield_reserved_priority is not None
            and row.yield_reserved_priority > query.priority
        ):
            return False
        if query.proxy_wait or query.proxy_enabled:
            proxy_id = self.state.bindings.get(row.id)
            bound = self.state.proxies[proxy_id] if proxy_id is not None else None
            if bound is not None and (not bound.enabled or (query.proxy_wait and bound.status != "alive")):
                return False
        return not any(self._exhausted_until(row.id, charge) for charge in query.quotas)

    def _exhausted_until(self, identity_id: UUID, charge: QuotaCharge) -> datetime | None:
        """Конец текущего окна квоты, если оно исчерпано."""
        start = _window_start(self._now, charge)
        used = self.state.quotas.get((identity_id, charge.kind, charge.window_seconds, start), 0)
        return start + timedelta(seconds=charge.window_seconds) if used >= charge.limit else None

    async def quota_reopens_at(self, query: CandidateQuery) -> datetime | None:
        free = replace(_with_among_ids(query), quotas=())
        reopens: list[datetime] = []
        for row in self.state.identities.values():
            if row.space_id != self.space.id or not self._candidate(row, free):
                continue
            ends = [end for charge in query.quotas if (end := self._exhausted_until(row.id, charge))]
            if ends:
                reopens.append(max(ends))
        return min(reopens, default=None)

    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
        query = _with_among_ids(query)
        if query.quotas:
            head = self._ordered_candidates(replace(query, quotas=()))
            if not head:
                return None
            first = head[0]
            if not any(self._exhausted_until(first.id, charge) for charge in query.quotas):
                return first
            self._demote_exhausted(query)
        candidates = self._ordered_candidates(query)
        return candidates[0] if candidates else None

    async def pick_candidate_reserving(
        self, query: CandidateQuery, reserve: int
    ) -> tuple[IdentityRow | None, bool]:
        if reserve == 0:
            return await self.pick_candidate(query), False
        candidates = self._ordered_candidates(_with_among_ids(query))
        if len(candidates) <= reserve:
            return None, bool(candidates)
        return candidates[0], False

    async def request_yield(
        self, query: CandidateQuery, priority: int, grace_seconds: float | None = None
    ) -> YieldRequest | None:
        admissible = replace(_with_among_ids(query), shared=True)
        victims = [
            lease
            for lease in self.state.leases.values()
            if lease.expires_at > self._now
            and lease.priority < priority
            and (identity := self._own(lease.identity_id)) is not None
            and self._candidate(identity, admissible)
        ]
        victims.sort(key=lambda lease: (lease.priority, lease.acquired_at, lease.identity_id))
        if not victims:
            return None
        victim = victims[0]
        if (
            grace_seconds is not None
            and victim.yield_requested_at is not None
            and victim.yield_requested_at + timedelta(seconds=grace_seconds) <= self._now
        ):
            history = self.state.history[victim.lease_id]
            self.state.history[victim.lease_id] = replace(
                history,
                released_at=self._now,
                duration=self._now - history.acquired_at,
                end_reason=str(LeaseEndReason.YIELDED),
            )
            self.state.leases.pop(victim.identity_id)
            identity = self._own(victim.identity_id)
            assert identity is not None
            self.state.identities[identity.id] = replace(
                identity,
                yield_reserved_priority=priority,
                yield_reserved_until=self._now + timedelta(seconds=max(1.0, grace_seconds)),
            )
            return YieldRequest(victim.lease_id, forced=True)
        if victim.yield_requested_priority is not None and victim.yield_requested_priority >= priority:
            return None
        updated = replace(victim, yield_requested_at=self._now, yield_requested_priority=priority)
        self.state.leases[victim.identity_id] = updated
        identity = self._own(victim.identity_id)
        assert identity is not None
        await self.emit(
            [
                EventDraft(
                    "lease.yield_requested",
                    actor="system",
                    service=identity.service,
                    identity_id=identity.id,
                    lease_id=victim.lease_id,
                    data={
                        "pool": victim.pool,
                        "holder": victim.holder,
                        "priority": victim.priority,
                        "requested_priority": priority,
                    },
                )
            ]
        )
        return YieldRequest(victim.lease_id)

    def _ordered_candidates(self, query: CandidateQuery) -> list[IdentityRow]:
        candidates = [
            row
            for row in self.state.identities.values()
            if row.space_id == self.space.id and self._candidate(row, query)
        ]
        # IMP-01 (D-209): наименее недавно арендованный, никогда не арендованные — первыми, затем по id
        order = self.state.lease_order
        candidates.sort(
            key=lambda row: (
                row.pick_order_at is not None,
                row.pick_order_at or self._now,
                order.get(row.id, 0),
                row.id,
            )
        )
        return candidates

    def _demote_exhausted(self, query: CandidateQuery) -> None:
        """Перенести исчерпанные аккаунты из головы порядка в хвост — тот же проход, что в PostgreSQL."""
        free = replace(query, quotas=())
        head = self._ordered_candidates(free)[:PICK_DEMOTE_CHUNK]
        exhausted = [
            row for row in head if any(self._exhausted_until(row.id, charge) for charge in query.quotas)
        ]
        for row in exhausted:
            self.state.identities[row.id] = replace(row, pick_order_at=self._now)
            self.state.lease_order[row.id] = self.state.next_lease_order
            self.state.next_lease_order += 1

    async def count_candidates(self, query: CandidateQuery, limit: int) -> int:
        query = _with_among_ids(query)
        return min(
            limit,
            sum(
                row.space_id == self.space.id and self._candidate(row, query)
                for row in self.state.identities.values()
            ),
        )

    async def count_live_candidates(self, query: CandidateQuery, limit: int) -> int:
        unrestricted = replace(_with_among_ids(query), shared=True)
        return min(
            limit,
            sum(
                row.space_id == self.space.id
                and self._candidate(row, unrestricted)
                and (lease := self.state.leases.get(row.id)) is not None
                and lease.expires_at > self._now
                for row in self.state.identities.values()
            ),
        )

    async def find_live_lease(self, idempotency_key: str) -> LeaseRow | None:
        return next(
            (
                lease
                for lease in self.state.leases.values()
                if lease.idempotency_key == idempotency_key
                and lease.expires_at > self._now
                and self._own(lease.identity_id)
            ),
            None,
        )

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
        current = self._own(identity.id)
        if current is None:
            raise LeaseUnavailable(
                f"аккаунт {identity.id} недоступен", reason=LeaseUnavailableReason.NO_CANDIDATES
            )
        reclaimed: LeaseRow | None = None
        old = self.state.leases.get(identity.id)
        if not shared and old is not None:
            if old.expires_at > self._now:
                raise LeaseUnavailable(
                    f"аккаунт {identity.id} уже в аренде", reason=LeaseUnavailableReason.BUSY
                )
            reclaimed = old
            history = self.state.history[old.lease_id]
            if history.released_at is None:
                self.state.history[old.lease_id] = replace(
                    history,
                    released_at=old.expires_at,
                    duration=old.expires_at - history.acquired_at,
                    end_reason=str(expiry_reason(history.hold_deadline, old.expires_at)),
                )
        # shared-аренды не вытесняют друг друга: fencing растёт только при эксклюзивном захвате (D-140)
        fencing = current.fencing_seq if shared else current.fencing_seq + 1
        # время захвата ставят обе аренды — по нему идёт LRU-выборка (IMP-01, D-209)
        self.state.identities[identity.id] = replace(
            current,
            fencing_seq=fencing,
            last_leased_at=self._now,
            pick_order_at=self._now,
            updated_at=self._now,
            yield_reserved_priority=None,
            yield_reserved_until=None,
        )
        self.state.lease_order[identity.id] = self.state.next_lease_order
        self.state.next_lease_order += 1
        expires_at = self._now + timedelta(seconds=ttl_seconds)
        hold_deadline: datetime | None = None
        if options.max_hold_seconds is not None:
            hold_deadline = self._now + timedelta(seconds=options.max_hold_seconds)
            expires_at = min(expires_at, hold_deadline)
        lease = LeaseRow(
            new_id(),  # публичный id аренды упорядочен по времени, как в PostgreSQL (R2-P10)
            uuid4(),  # token — секрет аренды, случайный
            identity.id,
            fencing,
            pool,
            holder,
            self._now,
            expires_at,
            idempotency_key,
            proxy_id,
            self._now,
            purpose=options.purpose,
            ref=options.ref,
            hold_deadline=hold_deadline,
            priority=options.priority,
        )
        if not shared:
            self.state.leases[identity.id] = lease
        self.state.history_spaces[lease.lease_id] = self.space.id
        self.state.history_tokens[lease.lease_id] = lease.token
        self.state.history[lease.lease_id] = LeaseHistoryRow(
            lease.lease_id,
            identity.id,
            fencing,
            pool,
            holder,
            proxy_id,
            self._now,
            self._now,
            None,
            None,
            None,
            {},
            0,
            0,
            None,
            purpose=options.purpose,
            ref=options.ref,
            hold_deadline=hold_deadline,
            priority=options.priority,
        )
        return lease, reclaimed

    async def extend_lease(
        self,
        lease: LeaseRow,
        ttl_seconds: float,
        counters: LeaseCounters,
        *,
        shared: bool,
        flush: bool = True,
    ) -> LeaseRenewal:
        if not shared:
            current = self.state.leases.get(lease.identity_id)
            if current is None or current.token != lease.token or current.expires_at <= self._now:
                return _gone(self.state, lease.token)
        history = self.state.history.get(lease.lease_id)
        if history is None or history.released_at is not None:
            return _gone(self.state, lease.token)
        expires_at = self._now + timedelta(seconds=ttl_seconds)
        if shared and history.hold_deadline is not None and history.hold_deadline <= self._now:
            return LeaseRenewal(None, LeaseEndReason.MAX_HOLD)  # потолок разделяемой — в истории
        if not shared and current is not None:
            expires_at = max(expires_at, current.expires_at)  # продление не укорачивает (D-213)
        if history.hold_deadline is not None:
            expires_at = min(expires_at, history.hold_deadline)  # и не выходит за потолок удержания
        if not shared and current is not None:
            self.state.leases[lease.identity_id] = replace(
                current, expires_at=expires_at, heartbeat_at=self._now
            )
            if not flush:
                return LeaseRenewal(expires_at, yield_requested=current.yield_requested_at is not None)
        self.state.history[lease.lease_id] = replace(
            history, last_heartbeat_at=self._now, **_merge_counters(history, counters)
        )
        return LeaseRenewal(
            expires_at,
            yield_requested=(not shared and current is not None and current.yield_requested_at is not None),
        )

    async def release_lease(
        self, lease: LeaseRow, reason: str, counters: LeaseCounters, *, shared: bool
    ) -> str:
        history = self.state.history.get(lease.lease_id)
        if history is None or history.released_at is not None:
            return "revoked" if history is not None and history.end_reason == "revoked" else "closed"
        released_at, end_reason, expired = self._now, reason, False
        if not shared:
            current = self.state.leases.get(lease.identity_id)
            if current is None or current.token != lease.token:
                return "closed"
            self.state.leases.pop(lease.identity_id)
            if current.expires_at <= self._now:  # срок вышел, аккаунт никто не взял (D-213)
                released_at, expired = current.expires_at, True
                end_reason = str(expiry_reason(history.hold_deadline, released_at))
        self.state.history[lease.lease_id] = replace(
            history,
            released_at=released_at,
            duration=released_at - history.acquired_at,
            end_reason=end_reason,
            **_merge_counters(history, counters),
        )
        return "expired" if expired else "released"

    async def get_throttle_state(self, identity_id: UUID, names: Sequence[str]) -> dict[str, datetime]:
        return {
            name: tat
            for name in names
            if (tat := self.state.throttle_state.get((identity_id, name))) is not None
        }

    async def save_throttle_state(self, identity_id: UUID, state: Mapping[str, datetime]) -> None:
        for name, tat in state.items():
            key = (identity_id, name)
            current = self.state.throttle_state.get(key)
            self.state.throttle_state[key] = max(current, tat) if current is not None else tat

    async def last_unclean_lease_end(self, identity_id: UUID) -> datetime | None:
        ends = [
            row.released_at
            for row in self.state.history.values()
            if row.identity_id == identity_id
            and row.end_reason in {"expired", "lost", "revoked"}
            and row.released_at is not None
            and self.state.history_spaces.get(row.lease_id) == self.space.id
        ]
        return max(ends, default=None)

    async def take_shared_throttle(
        self, identity_id: UUID, name: str, interval_seconds: float, burst: int
    ) -> float:
        key = (identity_id, name)
        tat = self.state.throttle_state.get(key, self._now)
        allowed_at = tat - timedelta(seconds=interval_seconds * (burst - 1))
        if self._now < allowed_at:
            return max(0.0, (allowed_at - self._now).total_seconds())
        self.state.throttle_state[key] = max(tat, self._now) + timedelta(seconds=interval_seconds)
        return 0.0

    async def lock_identities(self, identity_ids: Sequence[UUID]) -> None:
        """Транзакции MemoryStore и так сериализованы (D-124): блокировать нечего."""

    async def check_fence(self, fence: LeaseFence, *, lock: bool = True) -> None:
        identity = self._own(fence.identity_id)
        if identity is not None and identity.fencing_seq == fence.fencing:
            if fence.shared:
                history = self.state.history.get(fence.lease_id)
                if (
                    history is not None
                    and self.state.history_tokens.get(fence.lease_id) == fence.token
                    and history.identity_id == fence.identity_id
                    and history.released_at is None
                ):
                    return
            else:
                live = self.state.leases.get(fence.identity_id)
                if (
                    live is not None
                    and live.lease_id == fence.lease_id
                    and live.token == fence.token
                    and live.expires_at > self._now
                ):
                    return
        raise LeaseOwnershipLost(
            fence.lease_id, "fence", identity_id=fence.identity_id, fencing=fence.fencing
        )

    async def current_fencing(self, identity_id: UUID, *, lock: bool = False) -> int | None:
        row = self._own(identity_id)
        return row.fencing_seq if row else None

    async def revoke_lease(self, identity_id: UUID) -> LeaseRow | None:
        lease = self.state.leases.get(identity_id)
        if lease is None or lease.expires_at <= self._now or self._own(identity_id) is None:
            return None
        self.state.leases.pop(identity_id)
        identity = self.state.identities[identity_id]
        self.state.identities[identity_id] = replace(
            identity, fencing_seq=identity.fencing_seq + 1, updated_at=self._now
        )
        history = self.state.history[lease.lease_id]
        self.state.history[lease.lease_id] = replace(
            history, released_at=self._now, duration=self._now - history.acquired_at, end_reason="revoked"
        )
        return lease

    async def charge_quotas(
        self,
        identity_id: UUID,
        charges: Sequence[QuotaCharge],
        *,
        idempotency: IdempotencyRequest | None = None,
    ) -> ChargeOutcome:
        claim: IdempotencyClaim | None = None
        previous: _IdempotencyRecord | None = None
        if idempotency is not None:
            previous = self.state.operation_keys.get((idempotency.namespace, idempotency.key))
            claim = await self.claim_idempotency_key(idempotency)
            if not claim.claimed:
                return ChargeOutcome(duplicate=True, claim=claim)
        planned: list[tuple[tuple[UUID, str, int, datetime], int, QuotaCharge]] = []
        for charge in charges:
            start = _window_start(self._now, charge)
            key = (identity_id, charge.kind, charge.window_seconds, start)
            used = self.state.quotas.get(key, 0)
            if used >= charge.limit:
                if idempotency is not None:  # исчерпанная квота ключ не занимает
                    slot = (idempotency.namespace, idempotency.key)
                    if previous is None:
                        del self.state.operation_keys[slot]
                    else:
                        self.state.operation_keys[slot] = previous
                return ChargeOutcome(exhausted=QuotaWindow(charge.kind, charge.window_seconds, start, used))
            planned.append((key, used + 1, charge))
        reached: list[QuotaWindow] = []
        for key, used, charge in planned:
            self.state.quotas[key] = used
            if used >= charge.limit:
                reached.append(QuotaWindow(charge.kind, charge.window_seconds, key[3], used))
        return ChargeOutcome(reached=tuple(reached), claim=claim)

    async def bump_pacing(self, identity_id: UUID, charge: QuotaCharge) -> bool:
        start = _window_start(self._now, charge)
        key = (identity_id, charge.kind, charge.window_seconds, start)
        used = self.state.quotas.get(key, 0) + 1
        reached = used >= charge.limit
        self.state.quotas[key] = 0 if reached else used
        return reached

    async def quota_usage(self, identity_id: UUID) -> list[QuotaWindow]:
        rows = [
            QuotaWindow(kind, seconds, start, used)
            for (owner, kind, seconds, start), used in self.state.quotas.items()
            if owner == identity_id and start + timedelta(seconds=seconds) > self._now
        ]
        return sorted(rows, key=lambda row: (row.kind, row.window_seconds))

    async def claim_idempotency_key(self, request: IdempotencyRequest) -> IdempotencyClaim:
        slot = (request.namespace, request.key)
        old = self.state.operation_keys.get(slot)
        expired = old is not None and old.expires_at <= self._now
        if old is not None and not expired:
            same = old.operation == request.operation and (
                old.fingerprint_version == LEGACY_FINGERPRINT_VERSION
                or old.fingerprint == request.fingerprint_for(old.fingerprint_key_id)
            )
            if not same:
                raise IdempotencyConflict(old.operation)
            if old.outcome != "failed":
                return IdempotencyClaim(
                    False,
                    old.attempt,
                    old.outcome,
                    old.owner_identity_id,
                    old.result_type,
                    old.result_version,
                    deepcopy(old.result),
                )
        attempt = 1 if old is None or expired else old.attempt + 1
        self.state.operation_keys[slot] = _IdempotencyRecord(
            space_id=self.space.id,
            owner_identity_id=request.owner_identity_id,
            operation=request.operation,
            fingerprint_version=FINGERPRINT_VERSION,
            fingerprint=request.fingerprint,
            fingerprint_key_id=request.fingerprint_key_id,
            attempt=attempt,
            created_at=self._now if old is None or expired else old.created_at,
            expires_at=self._now + request.ttl,
        )
        return IdempotencyClaim(True, attempt, "failed" if attempt > 1 else None, request.owner_identity_id)

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
        old = self.state.operation_keys.get((namespace, key))
        if old is not None and old.space_id == self.space.id:
            self.state.operation_keys[(namespace, key)] = replace(
                old,
                outcome=outcome,
                result_type=result_type,
                result_version=result_version,
                result=deepcopy(dict(result)) if result is not None else None,
            )

    async def add_error_score(self, identity_id: UUID, delta: float) -> float:
        row = self._own(identity_id)
        if row is None:
            raise KeyError(identity_id)
        score = max(0.0, row.error_score + delta)
        self.state.identities[identity_id] = replace(row, error_score=score, updated_at=self._now)
        return score

    async def decay_error_score(self, identity_id: UUID, factor: float) -> None:
        row = self._own(identity_id)
        if row is not None and row.error_score > 0:
            score = row.error_score * factor
            self.state.identities[identity_id] = replace(
                row, error_score=0.0 if score < 0.01 else score, updated_at=self._now
            )

    async def set_session_status(self, identity_id: UUID, slot: str, status: str) -> None:
        key = (identity_id, slot)
        current = self._current_session(*key)
        if current is not None and self._own(identity_id):
            self.state.sessions[current.id] = replace(current, status=status, updated_at=self._now)

    async def insert_proxy(self, row: NewProxy) -> tuple[UUID, bool]:
        known = await self.proxy_by_fingerprint([row.fingerprint, *row.aliases])
        if known is not None:
            return known, False
        proxy = ProxyRow(
            id=row.id,
            space_id=self.space.id,
            pool=row.pool,
            scheme=row.scheme,
            host=row.host,
            port=row.port,
            fingerprint=row.fingerprint,
            status="alive",
            error_score=0.0,
            fail_streak=0,
            next_check_at=None,
            status_changed_at=self._now,
            status_source="command",
            checked_at=None,
            pinned_until=None,
            pending_observation=None,
            max_identities=row.max_identities,
            created_at=self._now,
            secret=row.secret,
            fingerprint_key_id=row.fingerprint_key_id,
        )
        self.state.proxies[row.id] = proxy
        self.state.proxy_load[row.id] = 0
        return row.id, True

    async def insert_proxies(self, rows: Sequence[NewProxy]) -> list[tuple[ProxyRow, bool]]:
        results: list[tuple[ProxyRow, bool]] = []
        for row in rows:
            proxy_id, created = await self.insert_proxy(row)
            results.append((self.state.proxies[proxy_id], created))
        return results

    async def get_proxy(self, proxy_id: UUID) -> ProxyRow | None:
        proxy = self.state.proxies.get(proxy_id)
        return proxy if proxy is not None and proxy.space_id == self.space.id else None

    async def update_proxy(
        self, proxy_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        row = await self.get_proxy(proxy_id)
        if row is None or any(getattr(row, key) != value for key, value in (expected or {}).items()):
            return False
        self.state.proxies[proxy_id] = replace(row, **values)
        return True

    async def add_proxy_error(self, proxy_id: UUID, weight: float) -> float | None:
        row = await self.get_proxy(proxy_id)
        if row is None or row.status != "alive":
            return None
        score = row.error_score + weight
        self.state.proxies[proxy_id] = replace(row, error_score=score)
        return score

    async def proxy_by_fingerprint(self, fingerprints: Sequence[str]) -> UUID | None:
        wanted = set(fingerprints)
        found = sorted(
            row.id
            for row in self.state.proxies.values()
            if row.space_id == self.space.id and row.fingerprint in wanted
        )
        return found[0] if found else None

    async def set_proxy_secret(
        self, proxy_id: UUID, fingerprint: str, secret: StoredPayload, *, fingerprint_key_id: str | None
    ) -> bool:
        row = await self.get_proxy(proxy_id)
        if row is None:
            return False
        self.state.proxies[proxy_id] = replace(
            row, fingerprint=fingerprint, fingerprint_key_id=fingerprint_key_id, secret=secret
        )
        return True

    async def set_proxy_fingerprint(self, proxy_id: UUID, fingerprint: str, key_id: str | None) -> bool:
        row = await self.get_proxy(proxy_id)
        taken = await self.proxy_by_fingerprint([fingerprint])
        if row is None or (taken is not None and taken != proxy_id):
            return False
        self.state.proxies[proxy_id] = replace(row, fingerprint=fingerprint, fingerprint_key_id=key_id)
        return True

    async def unbind_identity(self, identity_id: UUID) -> UUID | None:
        if self._own(identity_id) is None:
            return None
        proxy_id = self.state.bindings.pop(identity_id, None)
        if proxy_id is not None:
            self.state.binding_times.pop(identity_id, None)
            await self.adjust_proxy_load(proxy_id, -1)
        return proxy_id

    async def unbind_proxy(self, proxy_id: UUID) -> list[UUID]:
        if await self.get_proxy(proxy_id) is None:
            return []
        unbound = [identity_id for identity_id, bound in self.state.bindings.items() if bound == proxy_id]
        for identity_id in unbound:
            del self.state.bindings[identity_id]
            self.state.binding_times.pop(identity_id, None)
        self.state.proxy_load[proxy_id] = 0
        return unbound

    async def delete_proxy(self, proxy_id: UUID) -> list[UUID] | None:
        if await self.get_proxy(proxy_id) is None:
            return None
        unbound = [identity_id for identity_id, bound in self.state.bindings.items() if bound == proxy_id]
        for identity_id in unbound:
            del self.state.bindings[identity_id]
            self.state.binding_times.pop(identity_id, None)
        self.state.proxy_load.pop(proxy_id, None)
        del self.state.proxies[proxy_id]
        return unbound

    async def get_binding(self, identity_id: UUID) -> ProxyRow | None:
        proxy_id = self.state.bindings.get(identity_id)
        return await self.get_proxy(proxy_id) if proxy_id is not None and self._own(identity_id) else None

    async def choose_proxy(self, pool: str, max_identities: int | None, *, claim: bool) -> ProxyRow | None:
        candidates = []
        for proxy in self.state.proxies.values():
            capacity = proxy.max_identities if max_identities is None else max_identities
            load = self.state.proxy_load.get(proxy.id, 0)
            if proxy.space_id == self.space.id and proxy.pool == pool and proxy.issuable and load < capacity:
                candidates.append(proxy)
        candidates.sort(
            key=lambda proxy: (self.state.proxy_load.get(proxy.id, 0), proxy.error_score, proxy.id)
        )
        if not candidates:
            return None
        selected = candidates[0]
        if claim:
            self.state.proxy_load[selected.id] = self.state.proxy_load.get(selected.id, 0) + 1
        return selected

    async def bind_proxy(self, identity_id: UUID, proxy_id: UUID, previous: UUID | None) -> None:
        if self._own(identity_id) is None or await self.get_proxy(proxy_id) is None:
            return
        self.state.bindings[identity_id] = proxy_id
        self.state.binding_times[identity_id] = self._now
        if previous is not None and previous != proxy_id:
            self.state.proxy_load[previous] = max(0, self.state.proxy_load.get(previous, 0) - 1)

    async def rebind_proxy(self, identity_id: UUID, proxy_id: UUID) -> tuple[UUID | None, ProxyRow] | None:
        if self._own(identity_id) is None:
            return None
        target = await self.get_proxy(proxy_id)
        if target is None:
            return None
        previous = self.state.bindings.get(identity_id)
        if previous == proxy_id:
            return previous, target
        load = self.state.proxy_load.get(proxy_id, 0)
        if not target.issuable or load >= target.max_identities:
            return None
        self.state.proxy_load[proxy_id] = load + 1
        await self.bind_proxy(identity_id, proxy_id, previous)
        return previous, target

    async def adjust_proxy_load(self, proxy_id: UUID, delta: int) -> None:
        if await self.get_proxy(proxy_id) is not None:
            self.state.proxy_load[proxy_id] = max(0, self.state.proxy_load.get(proxy_id, 0) + delta)

    async def due_proxies(self, limit: int, *, pool: str | None = None) -> list[UUID]:
        def ready(row: ProxyRow) -> bool:
            if row.status == "quarantined":
                return row.next_check_at is not None and row.next_check_at <= self._now
            if row.status == "dead":
                return row.next_check_at is None or row.next_check_at <= self._now
            return False

        due = [
            row
            for row in self.state.proxies.values()
            if row.space_id == self.space.id
            and (pool is None or row.pool == pool)
            and ready(row)
            and row.enabled
            and (row.pinned_until is None or row.pinned_until <= self._now)
        ]
        never = datetime.min.replace(tzinfo=UTC)
        due.sort(key=lambda row: (row.next_check_at or never, row.id))
        return [row.id for row in due[:limit]]

    async def proxy_read_page(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
        pool: str | None,
        status: str | None,
        capacity: str | None,
        enabled: bool | None = None,
    ) -> list[ProxyReadRow]:
        result = []
        for proxy in self.state.proxies.values():
            if proxy.space_id != self.space.id:
                continue
            bound_count = sum(value == proxy.id for value in self.state.bindings.values())
            if pool is not None and proxy.pool != pool:
                continue
            if status is not None and proxy.status != status:
                continue
            if enabled is not None and proxy.enabled != enabled:
                continue
            if capacity == "available" and bound_count >= proxy.max_identities:
                continue
            if capacity == "full" and bound_count < proxy.max_identities:
                continue
            if capacity == "unbound" and bound_count != 0:
                continue
            in_lease_count = sum(
                row.proxy_id == proxy.id and row.released_at is None for row in self.state.history.values()
            )
            result.append(
                ProxyReadRow(
                    id=proxy.id,
                    pool=proxy.pool,
                    scheme=proxy.scheme,
                    host=proxy.host,
                    port=proxy.port,
                    status=proxy.status,
                    error_score=proxy.error_score,
                    fail_streak=proxy.fail_streak,
                    next_check_at=proxy.next_check_at,
                    status_changed_at=proxy.status_changed_at,
                    status_source=proxy.status_source,
                    checked_at=proxy.checked_at,
                    pinned_until=proxy.pinned_until,
                    pending_observation=proxy.pending_observation,
                    max_identities=proxy.max_identities,
                    bound_count=bound_count,
                    in_lease_count=in_lease_count,
                    created_at=proxy.created_at,
                    enabled=proxy.enabled,
                    disabled_reason=proxy.disabled_reason,
                    disabled_by=proxy.disabled_by,
                    disabled_at=proxy.disabled_at,
                )
            )
        result.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        if before is not None:
            result = [row for row in result if (row.created_at, row.id) < before]
        return result[:limit]

    async def proxy_bound_identities(self, proxy_id: UUID) -> list[ProxyBoundIdentityRow]:
        if await self.get_proxy(proxy_id) is None:
            return []
        rows = [
            ProxyBoundIdentityRow(
                identity_id=identity_id,
                service=identity.service,
                external_ref=identity.external_ref,
                bound_at=self.state.binding_times.get(identity_id, identity.updated_at),
            )
            for identity_id, bound_proxy_id in self.state.bindings.items()
            if bound_proxy_id == proxy_id and (identity := self._own(identity_id)) is not None
        ]
        return sorted(rows, key=lambda row: (row.bound_at, row.identity_id), reverse=True)

    async def proxy_read_one(self, proxy_id: UUID) -> ProxyReadRow | None:
        rows = await self.proxy_read_page(
            limit=max(1, len(self.state.proxies)),
            before=None,
            pool=None,
            status=None,
            capacity=None,
        )
        return next((row for row in rows if row.id == proxy_id), None)

    async def proxy_pool_capacities(self) -> list[ProxyPoolCapacityRow]:
        pools: dict[str, dict[str, int]] = {}
        for proxy in self.state.proxies.values():
            if proxy.space_id != self.space.id:
                continue
            values = pools.setdefault(
                proxy.pool,
                {
                    "proxies": 0,
                    "alive": 0,
                    "quarantined": 0,
                    "dead": 0,
                    "other": 0,
                    "max_identities": 0,
                    "bound_count": 0,
                    "in_lease_count": 0,
                    "disabled": 0,
                },
            )
            values["proxies"] += 1
            values["disabled"] += 0 if proxy.enabled else 1
            values[proxy.status if proxy.status in {"alive", "quarantined", "dead"} else "other"] += 1
            values["max_identities"] += proxy.max_identities
            values["bound_count"] += sum(value == proxy.id for value in self.state.bindings.values())
            values["in_lease_count"] += sum(
                row.proxy_id == proxy.id and row.released_at is None for row in self.state.history.values()
            )
        return [ProxyPoolCapacityRow(pool=pool, **values) for pool, values in sorted(pools.items())]

    async def proxy_binding_history(
        self,
        proxy_id: UUID,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> list[ProxyBindingHistoryRow]:
        if await self.get_proxy(proxy_id) is None:
            return []
        rows = []
        for event, space_id, _ in self.state.events:
            data = event.data
            if (
                space_id != self.space.id
                or event.type != "proxy.binding.changed"
                or str(proxy_id)
                not in {
                    str(event.proxy_id) if event.proxy_id is not None else "",
                    str(data.get("from_proxy_id") or ""),
                    str(data.get("to_proxy_id") or ""),
                }
            ):
                continue
            identity = self._own(event.identity_id) if event.identity_id is not None else None
            rows.append(
                ProxyBindingHistoryRow(
                    event_id=event.event_id,
                    identity_id=event.identity_id,
                    service=event.service,
                    external_ref=identity.external_ref if identity is not None else None,
                    from_proxy_id=_memory_uuid(data.get("from_proxy_id")),
                    to_proxy_id=_memory_uuid(data.get("to_proxy_id")),
                    reason=str(data.get("reason") or "unknown"),
                    pool=str(data.get("pool") or ""),
                    actor=event.actor,
                    occurred_at=event.occurred_at,
                )
            )
        rows.sort(key=lambda row: (row.occurred_at, row.event_id), reverse=True)
        if before is not None:
            rows = [row for row in rows if (row.occurred_at, row.event_id) < before]
        return rows[:limit]

    async def event_read_page(
        self,
        filters: EventReadFilter,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> list[EventReadRow]:
        rows = []
        for event, space_id, external in self.state.events:
            if space_id != self.space.id or event.type not in filters.allowed_types:
                continue
            if filters.severities and event.severity.value not in filters.severities:
                continue
            if filters.service is not None and event.service != filters.service:
                continue
            if filters.identity_id is not None and event.identity_id != filters.identity_id:
                continue
            if filters.proxy_id is not None and event.proxy_id != filters.proxy_id:
                continue
            if filters.lease_id is not None and event.lease_id != filters.lease_id:
                continue
            if filters.actor is not None and filters.actor not in event.actor:
                continue
            if filters.occurred_from is not None and event.occurred_at < filters.occurred_from:
                continue
            if filters.occurred_to is not None and event.occurred_at > filters.occurred_to:
                continue
            if before is not None and (event.occurred_at, event.event_id) >= before:
                continue
            rows.append(
                EventReadRow(
                    event_id=event.event_id,
                    type=event.type,
                    severity=event.severity.value,
                    service=event.service,
                    identity_id=event.identity_id,
                    proxy_id=event.proxy_id,
                    lease_id=event.lease_id,
                    sequence=event.sequence,
                    actor=event.actor,
                    data=event.data,
                    external=external,
                    occurred_at=event.occurred_at,
                    revision=event.revision,
                )
            )
        rows.sort(key=lambda row: (row.occurred_at, row.event_id), reverse=True)
        return rows[:limit]

    async def event_read_one(self, event_id: UUID) -> EventReadRow | None:
        for event, space_id, external in self.state.events:
            if space_id == self.space.id and event.event_id == event_id:
                return EventReadRow(
                    event_id=event.event_id,
                    type=event.type,
                    severity=event.severity.value,
                    service=event.service,
                    identity_id=event.identity_id,
                    proxy_id=event.proxy_id,
                    lease_id=event.lease_id,
                    sequence=event.sequence,
                    actor=event.actor,
                    data=event.data,
                    external=external,
                    occurred_at=event.occurred_at,
                    revision=event.revision,
                )
        return None

    async def load_verification(self, identity_id: UUID, factor: str) -> VerificationRow | None:
        return self.state.verifications.get((identity_id, factor)) if self._own(identity_id) else None

    async def save_verification(
        self, identity_id: UUID, factor: str, values: dict[str, Any], evidence: StoredPayload | None = None
    ) -> None:
        if self._own(identity_id) is None:
            return
        previous = self.state.verifications.get(
            (identity_id, factor), VerificationRow(0, None, None, None, None, None)
        )
        # replace, а не asdict: asdict превращал сохранённые доказательства в dict (R2-P07)
        changes = dict(values)
        if evidence is not None:
            changes["evidence"] = evidence
        self.state.verifications[(identity_id, factor)] = replace(previous, **changes)

    async def due_regions(self, region: str, limit: int) -> list[tuple[UUID, str]]:
        rows = [
            (identity_id, key, state.until)
            for (identity_id, name, key), state in self.state.regions.items()
            if name == region
            and state.until is not None
            and state.until <= self._now
            and self._own(identity_id) is not None
        ]
        rows.sort(key=lambda item: item[2] or self._now)
        return [(identity_id, key) for identity_id, key, _ in rows[:limit]]

    async def expired_factors(self, identity_id: UUID) -> list[str]:
        if self._own(identity_id) is None:
            return []
        return sorted(
            key
            for (owner, region, key), state in self.state.regions.items()
            if owner == identity_id
            and region == "verification"
            and state.state == "verified"
            and state.until is not None
            and state.until <= self._now
        )

    async def stale_identities(
        self, service: str, region: str, key: str, older_than_seconds: float, limit: int
    ) -> list[UUID]:
        threshold = self._now - timedelta(seconds=older_than_seconds)
        never = (datetime.min.replace(tzinfo=UTC), 0)
        rows = []
        for identity in self.state.identities.values():
            if (
                identity.space_id != self.space.id
                or identity.service != service
                or identity.lifecycle != "ready"  # как в PostgreSQL: provisioning и retired не кандидаты
            ):
                continue
            state = self.state.regions.get((identity.id, region, key))
            if state is None or state.synced_at is None or state.synced_at < threshold:
                attempted = self.state.probe_attempts.get(identity.id, never)
                synced = state.synced_at if state is not None and state.synced_at is not None else never[0]
                rows.append((attempted, synced, identity.id))
        rows.sort()
        return [identity_id for _, _, identity_id in rows[:limit]]

    async def mark_probe_attempts(self, identity_ids: Sequence[UUID]) -> None:
        if not identity_ids:
            return
        order = self.state.next_probe_attempt
        self.state.next_probe_attempt += 1
        for identity_id in identity_ids:
            if self._own(identity_id) is not None:
                self.state.probe_attempts[identity_id] = (self._now, order)

    @staticmethod
    def _contains(container: Any, expected: Any) -> bool:
        if isinstance(expected, dict) and isinstance(container, dict):
            return all(
                key in container and MemoryTx._contains(container[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list) and isinstance(container, list):
            return all(item in container for item in expected)
        return bool(container == expected)

    def _matches_search(self, row: IdentityRow, flt: SearchFilter) -> bool:
        if flt.exact_id is not None and row.id != flt.exact_id:
            return False
        if flt.short_id is not None and not row.id.hex.endswith(flt.short_id):
            return False
        if flt.services and row.service not in flt.services:
            return False
        if flt.tags and not flt.tags.issubset(row.tags):
            return False
        for column, values in flt.regions.items():
            if getattr(row, column) not in values:
                return False
        if flt.any_region and not (
            {row.lifecycle, row.standing, row.auth_state, row.availability} & flt.any_region
        ):
            return False
        for source, variants in flt.contains:
            value = getattr(row, source)
            if not any(self._contains(value, variant) for variant in variants):
                return False
        haystack = row.search_text.casefold()
        if not all(term.casefold() in haystack for term in flt.terms):
            return False
        lease = self.state.leases.get(row.id)
        is_leased = lease is not None and lease.expires_at > self._now
        if flt.leased is not None and is_leased != flt.leased:
            return False
        has_open_challenge = any(
            challenge.identity_id == row.id and challenge.resolved_at is None
            for challenge in self.state.challenges.values()
        )
        needs_attention = (
            row.auth_state == AuthState.CHALLENGE or bool(row.requirements) or has_open_challenge
        )
        if flt.attention is not None and needs_attention != flt.attention:
            return False
        is_available = row.availability == Availability.AVAILABLE or (
            row.availability in (Availability.PAUSED, Availability.COOLING)
            and row.availability_until is not None
            and row.availability_until <= self._now
        )
        is_eligible = (
            row.lifecycle == Lifecycle.READY
            and row.standing in ("good", "limited")
            and row.auth_state == AuthState.AUTHENTICATED
            and is_available
        )
        if flt.eligible is not None and is_eligible != flt.eligible:
            return False
        regions = [
            (region, key, state)
            for (owner, region, key), state in self.state.regions.items()
            if owner == row.id
        ]
        has_override = any(
            state.pinned_until is not None and state.pinned_until > self._now for _, _, state in regions
        )
        if flt.has_override is not None and has_override != flt.has_override:
            return False
        if flt.credential_unverified is not None:
            waiting = any(
                record.lifecycle == "active"
                and (record.expires_at is None or record.expires_at > self._now)
                and record.last_verified_at is None
                and record.last_rejected_at is None
                for (owner, _, _), (_, record) in self.state.credentials.items()
                if owner == row.id
            )
            if waiting != flt.credential_unverified:
                return False
        if flt.session_unverified is not None:
            pending = any(
                record.identity_id == row.id
                and record.status != SessionStatus.REVOKED
                and record.last_verified_at is None
                and record.last_rejected_at is None
                for record in self.state.sessions.values()
            )
            if pending != flt.session_unverified:
                return False
        if flt.requirement is not None and not any(
            isinstance(item, dict) and item.get("code") == flt.requirement for item in row.requirements
        ):
            return False
        if flt.factor is not None and flt.factor not in row.verified_factors:
            return False
        if flt.factor_state is not None and not any(
            region == "verification"
            and state.state == flt.factor_state
            and (flt.factor is None or key == flt.factor)
            for region, key, state in regions
        ):
            return False
        if flt.tier_factors:
            tier_factors = flt.tier_factors.get(row.service)
            if tier_factors is None or not tier_factors.issubset(row.verified_factors):
                return False
        if flt.proxy is not None:
            proxy_id = self.state.bindings.get(row.id)
            proxy = self.state.proxies.get(proxy_id) if proxy_id is not None else None
            wanted = flt.proxy.casefold()
            if proxy is None or not (
                wanted in proxy.host.casefold() or str(proxy.id).casefold().startswith(wanted)
            ):
                return False
        is_linked = any(
            identity_id == row.id or linked_id == row.id
            for (identity_id, _), linked_id in self.state.links.items()
        )
        if flt.linked is not None and is_linked != flt.linked:
            return False
        if flt.without_link and any(
            source == row.id and role in flt.without_link for source, role in self.state.links
        ):
            return False
        if flt.not_target_of is not None and self._is_target_of(row.id, flt.not_target_of):
            return False
        if flt.is_target_of is not None and not self._is_target_of(row.id, flt.is_target_of):
            return False
        if flt.created_from is not None and row.created_at < flt.created_from:
            return False
        if flt.created_to is not None and row.created_at >= flt.created_to:
            return False
        history = [item for item in self.state.history.values() if item.identity_id == row.id]
        if (flt.last_leased_from is not None or flt.last_leased_to is not None) and not any(
            (flt.last_leased_from is None or item.acquired_at >= flt.last_leased_from)
            and (flt.last_leased_to is None or item.acquired_at < flt.last_leased_to)
            for item in history
        ):
            return False
        if (flt.synced_from is not None or flt.synced_to is not None) and not any(
            state.synced_at is not None
            and (flt.synced_from is None or state.synced_at >= flt.synced_from)
            and (flt.synced_to is None or state.synced_at < flt.synced_to)
            for _, _, state in regions
        ):
            return False
        return not (flt.standing_changed_from is not None or flt.standing_changed_to is not None) or any(
            region == "standing"
            and (flt.standing_changed_from is None or state.changed_at >= flt.standing_changed_from)
            and (flt.standing_changed_to is None or state.changed_at < flt.standing_changed_to)
            for region, _, state in regions
        )

    def _is_target_of(self, identity_id: UUID, predicate: tuple[str, str | None]) -> bool:
        role, from_service = predicate
        return any(
            linked_id == identity_id
            and link_role == role
            and (
                from_service is None
                or (
                    (source_row := self.state.identities.get(source)) is not None
                    and source_row.space_id == self.space.id
                    and source_row.service == from_service
                )
            )
            for (source, link_role), linked_id in self.state.links.items()
        )

    async def tag_counts(self, *, service: str | None, limit: int) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for row in self.state.identities.values():
            if row.space_id == self.space.id and (service is None or row.service == service):
                for tag in row.tags:
                    counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]

    async def search_identities(
        self,
        flt: SearchFilter,
        *,
        limit: int,
        after: tuple[Any, UUID] | None,
        count_limit: int | None,
    ) -> tuple[list[IdentityRow], int | None]:
        rows = [
            row
            for row in self.state.identities.values()
            if row.space_id == self.space.id and self._matches_search(row, flt)
        ]
        rows.sort(key=lambda row: row.id, reverse=True)
        non_null = [row for row in rows if self._search_sort_value(row, flt) is not None]
        nulls = [row for row in rows if self._search_sort_value(row, flt) is None]
        non_null.sort(
            key=lambda row: self._search_sort_value(row, flt),
            reverse=flt.sort_desc,
        )
        rows = non_null + nulls
        count = None if count_limit is None else min(len(rows), count_limit + 1)
        if after is not None:
            after_value, after_id = after
            if after_value is None:
                rows = [
                    row for row in rows if self._search_sort_value(row, flt) is None and row.id < after_id
                ]
            else:
                remaining: list[IdentityRow] = []
                for row in rows:
                    value = self._search_sort_value(row, flt)
                    if (
                        value is None
                        or (value == after_value and row.id < after_id)
                        or (flt.sort_desc and value < after_value)
                        or (not flt.sort_desc and value > after_value)
                    ):
                        remaining.append(row)
                rows = remaining
        return rows[:limit], count

    def _search_sort_value(self, row: IdentityRow, flt: SearchFilter) -> Any:
        if flt.sort_by.startswith("profile:"):
            return profile_sort_text(row.profile.get(flt.sort_by.partition(":")[2]))
        if flt.sort_by == "eligible":
            return int(
                row.lifecycle == "ready"
                and row.standing in {"good", "limited"}
                and row.auth_state == "authenticated"
                and (
                    row.availability == "available"
                    or (
                        row.availability in {"paused", "cooling"}
                        and row.availability_until is not None
                        and row.availability_until <= self._now
                    )
                )
            )
        if flt.sort_by == "leased":
            lease = self.state.leases.get(row.id)
            return int(lease is not None and lease.expires_at > self._now)
        if flt.sort_by == "last_leased_at":
            values = [
                history.acquired_at
                for history in self.state.history.values()
                if history.identity_id == row.id
            ]
            return max(values, default=None)
        if flt.sort_by == "synced_at":
            values = [
                state.synced_at
                for (identity_id, _, _), state in self.state.regions.items()
                if identity_id == row.id and state.synced_at is not None
            ]
            return max(values, default=None)
        if flt.sort_by == "verification_tier":
            factors = frozenset(row.verified_factors)
            return max(
                (
                    index
                    for index, required in enumerate(flt.sort_tiers.get(row.service, ()))
                    if required <= factors
                ),
                default=None,
            )
        return getattr(row, flt.sort_by)

    async def insert_lease_logs(self, rows: Sequence[dict[str, Any]]) -> None:
        for row in rows:
            identity_id = row.get("identity_id")
            if isinstance(identity_id, UUID) and self._own(identity_id):
                self.state.lease_logs.append(deepcopy(row) | {"space_id": self.space.id})

    async def list_regions(self, identity_id: UUID) -> list[tuple[str, str, RegionState]]:
        if self._own(identity_id) is None:
            return []
        return sorted(
            [
                (region, key, state)
                for (owner, region, key), state in self.state.regions.items()
                if owner == identity_id
            ],
            key=lambda item: (item[0], item[1]),
        )

    async def current_lease(self, identity_id: UUID) -> LeaseRow | None:
        lease = self.state.leases.get(identity_id)
        return (
            lease if lease is not None and lease.expires_at > self._now and self._own(identity_id) else None
        )

    async def data_namespaces(self, identity_id: UUID) -> list[tuple[str, int, int, datetime]]:
        if self._own(identity_id) is None:
            return []
        return sorted(
            [
                (namespace, row.schema_version, row.version, row.updated_at)
                for (owner, namespace), row in self.state.data.items()
                if owner == identity_id
            ]
        )

    async def region_counts(self, service: str | None) -> list[tuple[str, str, str, int]]:
        counts: dict[tuple[str, str, str], int] = defaultdict(int)
        for row in self.state.identities.values():
            if row.space_id != self.space.id or (service is not None and row.service != service):
                continue
            for column in ("lifecycle", "standing", "auth_state", "availability"):
                counts[(row.service, column, str(getattr(row, column)))] += 1
        return [(*key, count) for key, count in sorted(counts.items())]

    async def overview_snapshot(self) -> OverviewSnapshot:
        rows = [row for row in self.state.identities.values() if row.space_id == self.space.id]
        open_challenge_ids = {
            row.identity_id
            for row in self.state.challenges.values()
            if row.resolved_at is None and self._own(row.identity_id) is not None
        }
        service_names = sorted({row.service for row in rows})
        services: list[OverviewServiceRow] = []
        for service in service_names:
            owned = [row for row in rows if row.service == service]
            services.append(
                OverviewServiceRow(
                    service=service,
                    total=len(owned),
                    eligible=sum(
                        row.lifecycle == "ready"
                        and row.standing in {"good", "limited"}
                        and row.auth_state == "authenticated"
                        and (
                            row.availability == "available"
                            or (
                                row.availability in {"paused", "cooling"}
                                and row.availability_until is not None
                                and row.availability_until <= self._now
                            )
                        )
                        for row in owned
                    ),
                    leased=sum(
                        (lease := self.state.leases.get(row.id)) is not None and lease.expires_at > self._now
                        for row in owned
                    ),
                    heavy=sum(
                        row.standing in {"frozen", "banned", "deleted"}
                        or row.auth_state == "session_invalid"
                        or row.availability == "quarantined"
                        for row in owned
                    ),
                    open_challenges=sum(row.id in open_challenge_ids for row in owned),
                    action_required=sum(
                        row.id in open_challenge_ids
                        or row.auth_state == "challenge"
                        or bool(row.requirements)
                        for row in owned
                    ),
                    added_24h=sum(row.created_at >= self._now - timedelta(days=1) for row in owned),
                    added_7d=sum(row.created_at >= self._now - timedelta(days=7) for row in owned),
                )
            )

        factor_sets: dict[tuple[str, tuple[str, ...]], int] = defaultdict(int)
        for row in rows:
            factor_sets[(row.service, tuple(sorted(row.verified_factors)))] += 1

        verification_states: dict[tuple[str, str, str], int] = defaultdict(int)
        by_id = {row.id: row for row in rows}
        for (identity_id, region, factor), state in self.state.regions.items():
            identity = by_id.get(identity_id)
            if identity is not None and region == "verification":
                verification_states[(identity.service, factor, state.state)] += 1

        requirement_counts: dict[tuple[str, str], int] = defaultdict(int)
        requirement_deadlines: dict[tuple[str, str], datetime] = {}
        for row in rows:
            seen: set[str] = set()
            for requirement in row.requirements:
                if not isinstance(requirement, dict) or not isinstance(requirement.get("code"), str):
                    continue
                code = requirement["code"]
                key = (row.service, code)
                if code not in seen:
                    requirement_counts[key] += 1
                    seen.add(code)
                deadline = _as_datetime(requirement.get("deadline"))
                if deadline is not None and (
                    key not in requirement_deadlines or deadline < requirement_deadlines[key]
                ):
                    requirement_deadlines[key] = deadline

        return OverviewSnapshot(
            services=tuple(services),
            factor_sets=tuple((*key, count) for key, count in sorted(factor_sets.items())),
            verification_states=tuple((*key, count) for key, count in sorted(verification_states.items())),
            requirements=tuple(
                OverviewRequirementRow(service, code, count, requirement_deadlines.get((service, code)))
                for (service, code), count in sorted(requirement_counts.items())
            ),
        )

    async def attention_rows(self) -> list[AttentionIdentityRow]:
        result = []
        for identity in self.state.identities.values():
            if identity.space_id != self.space.id:
                continue
            regions = tuple(
                (region, key, state)
                for (owner, region, key), state in self.state.regions.items()
                if owner == identity.id
            )
            challenge = next(
                (
                    row
                    for row in self.state.challenges.values()
                    if row.identity_id == identity.id and row.resolved_at is None
                ),
                None,
            )
            region_attention = any(
                (region == "verification" and state.state == "under_review")
                or (
                    state.pinned_until is not None
                    and state.pinned_until > self._now
                    and state.raw_status is not None
                    and state.raw_status != state.state
                )
                for region, _, state in regions
            )
            if not (
                identity.requirements
                or identity.availability == Availability.QUARANTINED
                or challenge is not None
                or region_attention
            ):
                continue
            result.append(
                AttentionIdentityRow(
                    id=identity.id,
                    service=identity.service,
                    external_ref=identity.external_ref,
                    availability=identity.availability,
                    error_score=identity.error_score,
                    requirements=tuple(item for item in identity.requirements if isinstance(item, dict)),
                    regions=regions,
                    challenge=(
                        AttentionChallengeRow(
                            challenge.id,
                            challenge.kind,
                            challenge.hint,
                            challenge.created_at,
                            challenge.expires_at,
                        )
                        if challenge is not None
                        else None
                    ),
                    updated_at=identity.updated_at,
                )
            )
        return result

    async def lease_history(
        self, identity_id: UUID, *, limit: int, before: datetime | None
    ) -> list[LeaseHistoryRow]:
        rows = [
            row
            for row in self.state.history.values()
            if row.identity_id == identity_id
            and self.state.history_spaces.get(row.lease_id) == self.space.id
            and (before is None or row.acquired_at < before)
        ]
        return sorted(rows, key=lambda row: row.acquired_at, reverse=True)[:limit]

    async def lease_stats(self, identity_id: UUID, since: datetime) -> LeaseStatsRow:
        rows = [
            row
            for row in self.state.history.values()
            if row.identity_id == identity_id and row.acquired_at >= since
        ]
        durations = [row.duration or (self._now - row.acquired_at) for row in rows]
        total = sum(durations, timedelta())
        reasons: dict[str, int] = defaultdict(int)
        operations: dict[str, int] = defaultdict(int)
        for row in rows:
            reasons[row.end_reason or "active"] += 1  # открытая аренда — как в PostgreSQL (R2-P18)
            for kind, count in row.operations.items():
                operations[kind] += count
        return LeaseStatsRow(
            len(rows),
            total,
            total / len(durations) if durations else None,
            max(durations) if durations else None,
            dict(reasons),
            dict(operations),
            sum(row.succeeded for row in rows),
            sum(row.failed for row in rows),
        )

    def _lease_read_row(self, row: LeaseHistoryRow) -> LeaseReadRow:
        identity = self.state.identities[row.identity_id]
        live = self.state.leases.get(row.identity_id)
        own = live is not None and live.lease_id == row.lease_id
        expires_at = live.expires_at if live is not None and own else None
        values = asdict(row)
        if live is not None and own:  # heartbeat — в ih_leases, история раз в stats_flush (D-210)
            values["last_heartbeat_at"] = live.heartbeat_at
        return LeaseReadRow(
            **values,
            service=identity.service,
            external_ref=identity.external_ref,
            expires_at=expires_at,
        )

    async def lease_read_rows(self, since: datetime) -> list[LeaseReadRow]:
        rows = [
            row
            for row in self.state.history.values()
            if self._own(row.identity_id)
            and (row.acquired_at >= since or row.released_at is None or row.released_at >= since)
        ]
        return [
            self._lease_read_row(row) for row in sorted(rows, key=lambda item: item.acquired_at, reverse=True)
        ]

    async def lease_read_page(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
        pool: str | None,
        service: str | None,
        holder: str | None,
    ) -> list[LeaseReadRow]:
        rows = []
        for row in self.state.history.values():
            identity = self._own(row.identity_id)
            if identity is None:
                continue
            if pool is not None and row.pool != pool:
                continue
            if service is not None and identity.service != service:
                continue
            if holder is not None and row.holder != holder:
                continue
            if before is not None and (row.acquired_at, row.lease_id) >= before:
                continue
            rows.append(row)
        rows.sort(key=lambda item: (item.acquired_at, item.lease_id), reverse=True)
        return [self._lease_read_row(row) for row in rows[:limit]]

    async def identities_page(self, service: str, after: UUID | None, limit: int) -> list[IdentityRow]:
        rows = [
            row
            for row in self.state.identities.values()
            if row.space_id == self.space.id and row.service == service and (after is None or row.id > after)
        ]
        return sorted(rows, key=lambda row: row.id)[:limit]

    async def open_challenge(
        self,
        challenge_id: UUID,
        identity_id: UUID,
        kind: str,
        hint: str,
        context: StoredPayload,
        expires_at: datetime | None,
    ) -> bool:
        if self._own(identity_id) is None or await self.open_challenge_of(identity_id) is not None:
            return False
        self.state.challenges[challenge_id] = ChallengeRow(
            challenge_id,
            identity_id,
            kind,
            hint,
            context,
            None,
            None,
            None,
            self._now,
            expires_at,
            None,
            None,
        )
        return True

    async def get_challenge(self, challenge_id: UUID) -> ChallengeRow | None:
        row = self.state.challenges.get(challenge_id)
        return row if row is not None and self._own(row.identity_id) else None

    async def open_challenge_of(self, identity_id: UUID) -> ChallengeRow | None:
        return (
            next(
                (
                    row
                    for row in self.state.challenges.values()
                    if row.identity_id == identity_id and row.resolved_at is None
                ),
                None,
            )
            if self._own(identity_id)
            else None
        )

    async def answer_challenge(
        self,
        challenge_id: UUID,
        nonce: bytes | None = None,
        ciphertext: bytes | None = None,
        *,
        plaintext: str | None = None,
    ) -> bool:
        row = await self.get_challenge(challenge_id)
        if row is None or row.resolved_at is not None:
            return False
        self.state.challenges[challenge_id] = replace(
            row,
            answer_nonce=nonce,
            answer_ciphertext=ciphertext,
            answer_payload=plaintext,
            answered_at=self._now,
        )
        return True

    async def resolve_challenge(self, challenge_id: UUID) -> bool:
        row = await self.get_challenge(challenge_id)
        if row is None or row.resolved_at is not None:
            return False
        self.state.challenges[challenge_id] = replace(
            row, resolved_at=self._now, answer_nonce=None, answer_ciphertext=None, answer_payload=None
        )
        return True

    async def expired_challenges(self, limit: int) -> list[ChallengeRow]:
        rows = [
            row
            for row in self.state.challenges.values()
            if self._own(row.identity_id)
            and row.resolved_at is None
            and row.expires_at is not None
            and row.expires_at <= self._now
        ]
        return sorted(rows, key=lambda row: row.expires_at or self._now)[:limit]

    async def get_credentials(self, identity_id: UUID) -> StoredPayload | None:
        found = await self.get_credential(identity_id, "default", "default")
        return found[0] if found is not None else None

    async def get_session(self, identity_id: UUID, slot: str) -> tuple[StoredPayload, str] | None:
        record = self._current_session(identity_id, slot) if self._own(identity_id) else None
        return (record.payload, record.status) if record is not None else None

    async def access_metadata(
        self, identity_id: UUID
    ) -> tuple[CredentialMetadataRow, list[SessionMetadataRow]]:
        if self._own(identity_id) is None:
            return CredentialMetadataRow(False, None, None), []
        default = self.state.credentials.get((identity_id, "default", "default"))
        credential = CredentialMetadataRow(
            default is not None,
            default[1].version if default is not None else None,
            default[1].updated_at if default is not None else None,
            default[1].last_verified_at if default is not None else None,
            default[1].last_rejected_at if default is not None else None,
            default[1].reject_code if default is not None else None,
        )
        current = [self._current_session(owner, slot) for owner, slot in sorted(self.state.session_slots)]
        sessions = [
            SessionMetadataRow(
                r.slot,
                r.status,
                r.expires_at,
                r.version,
                r.written_by_fencing,
                r.updated_at,
                r.last_verified_at,
                r.last_rejected_at,
                r.reject_code,
            )
            for r in current
            if r is not None and r.identity_id == identity_id
        ]
        return credential, sessions

    async def insert_webhook_endpoint(self, row: NewWebhookEndpoint) -> WebhookEndpointRow:
        endpoint = WebhookEndpointRow(
            row.id,
            self.space.id,
            self.space.slug,
            row.url,
            row.description,
            list(row.event_types),
            deepcopy(row.filter),
            row.min_severity,
            True,
            None,
            0,
            self._now,
            row.secret,
            row.ordered,
        )
        self.state.webhook_endpoints[row.id] = endpoint
        return endpoint

    async def get_webhook_endpoint(self, endpoint_id: UUID) -> WebhookEndpointRow | None:
        row = self.state.webhook_endpoints.get(endpoint_id)
        return row if row is not None and row.space_id == self.space.id else None

    async def update_webhook_secret(self, endpoint_id: UUID, secret: StoredPayload) -> bool:
        row = await self.get_webhook_endpoint(endpoint_id)
        if row is None:
            return False
        self.state.webhook_endpoints[endpoint_id] = replace(row, secret=secret)
        return True

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
    ) -> bool:
        row = await self.get_webhook_endpoint(endpoint_id)
        if row is None:
            return False
        self.state.webhook_endpoints[endpoint_id] = replace(
            row,
            url=url,
            description=description,
            event_types=list(event_types),
            filter=deepcopy(filter),
            min_severity=min_severity,
            ordered=row.ordered if ordered is None else ordered,
        )
        return True

    async def delete_webhook_endpoint(self, endpoint_id: UUID) -> bool:
        row = await self.get_webhook_endpoint(endpoint_id)
        if row is None:
            return False
        del self.state.webhook_endpoints[endpoint_id]
        for delivery_id in [
            key
            for key, delivery in self.state.webhook_deliveries.items()
            if delivery.endpoint_id == endpoint_id
        ]:
            del self.state.webhook_deliveries[delivery_id]
        return True

    async def set_webhook_enabled(self, endpoint_id: UUID, enabled: bool, reason: str | None) -> bool:
        row = await self.get_webhook_endpoint(endpoint_id)
        if row is None:
            return False
        self.state.webhook_endpoints[endpoint_id] = replace(
            row,
            enabled=enabled,
            disabled_reason=None if enabled else reason,
            failure_streak=0 if enabled else row.failure_streak,
        )
        if enabled:
            _resume_deliveries(self.state, endpoint_id, self._now)
        else:
            _pause_deliveries(self.state, endpoint_id)
        return True

    async def retry_webhook_delivery(self, delivery_id: int) -> bool:
        row = self.state.webhook_deliveries.get(delivery_id)
        endpoint = await self.get_webhook_endpoint(row.endpoint_id) if row else None
        if row is None or endpoint is None:
            return False
        self.state.webhook_deliveries[delivery_id] = replace(
            row,
            status="pending",
            attempt=0,
            next_attempt_at=self._now,
            response_code=None,
            response_ms=None,
            error=None,
            delivered_at=None,
        )
        self.state.webhook_endpoints[endpoint.id] = replace(
            endpoint, enabled=True, disabled_reason=None, failure_streak=0
        )
        _resume_deliveries(self.state, endpoint.id, self._now)
        return True

    async def webhook_deliveries(self, endpoint_id: UUID, limit: int) -> list[WebhookDeliveryRow]:
        if await self.get_webhook_endpoint(endpoint_id) is None:
            return []
        rows = [row for row in self.state.webhook_deliveries.values() if row.endpoint_id == endpoint_id]
        return sorted(rows, key=lambda row: row.id, reverse=True)[:limit]

    async def blocked_webhook_deliveries(self) -> dict[UUID, int]:
        counts: dict[UUID, int] = {}
        for row in self.state.webhook_deliveries.values():
            endpoint = self.state.webhook_endpoints.get(row.endpoint_id)
            if endpoint is None or endpoint.space_id != self.space.id:
                continue
            if row.status != "succeeded" and self.store._blocked_by_earlier_delivery(row):
                counts[row.endpoint_id] = counts.get(row.endpoint_id, 0) + 1
        return counts

    async def webhook_endpoint_reads(self) -> list[WebhookEndpointReadRow]:
        return [
            WebhookEndpointReadRow(
                row.id,
                row.url,
                row.description,
                deepcopy(row.event_types),
                deepcopy(row.filter),
                row.min_severity,
                row.enabled,
                row.disabled_reason,
                row.failure_streak,
                row.created_at,
                row.ordered,
            )
            for row in sorted(
                self.state.webhook_endpoints.values(),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
            if row.space_id == self.space.id
        ]

    async def webhook_delivery_read_page(
        self, *, limit: int, before: tuple[datetime, int] | None = None
    ) -> list[WebhookDeliveryRow]:
        endpoint_ids = {
            row.id for row in self.state.webhook_endpoints.values() if row.space_id == self.space.id
        }
        rows = [
            row
            for row in self.state.webhook_deliveries.values()
            if row.endpoint_id in endpoint_ids and (before is None or (row.created_at, row.id) < before)
        ]
        return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)[:limit]

    async def insert_notification_channel(self, row: NewNotificationChannel) -> NotificationChannelRow:
        channel = NotificationChannelRow(row.id, row.title, deepcopy(row.config), True, self._now)
        self.state.notification_channels[row.id] = (channel, row.secret, self.space.id)
        return channel

    async def notification_channels(self) -> list[NotificationChannelRow]:
        return sorted(
            [
                channel
                for channel, _, space_id in self.state.notification_channels.values()
                if space_id == self.space.id
            ],
            key=lambda row: (row.created_at, row.id),
        )

    async def get_notification_channel_secret(self, channel_id: UUID) -> NotificationChannelSecretRow | None:
        item = self.state.notification_channels.get(channel_id)
        if item is None or item[2] != self.space.id:
            return None
        channel, secret, _ = item
        return NotificationChannelSecretRow(channel.id, deepcopy(channel.config), secret)

    async def update_notification_channel(
        self, channel_id: UUID, values: dict[str, Any], secret: StoredPayload | None
    ) -> bool:
        item = self.state.notification_channels.get(channel_id)
        if item is None:
            return False
        channel, old_secret, space_id = item
        if space_id != self.space.id:
            return False
        self.state.notification_channels[channel_id] = (
            replace(channel, **values),
            secret or old_secret,
            space_id,
        )
        return True

    async def delete_notification_channel(self, channel_id: UUID) -> bool:
        item = self.state.notification_channels.get(channel_id)
        if item is None or item[2] != self.space.id:
            return False
        del self.state.notification_channels[channel_id]
        for rule_id in [
            rule.id for rule in self.state.notification_rules.values() if rule.channel_id == channel_id
        ]:
            del self.state.notification_rules[rule_id]
            self.state.notification_rule_spaces.pop(rule_id, None)
        return True

    async def insert_notification_rule(self, row: NewNotificationRule) -> NotificationRuleRow:
        item = self.state.notification_channels.get(row.channel_id)
        if item is None or item[2] != self.space.id:
            raise NotFound("notification channel", row.channel_id)
        rule = NotificationRuleRow(
            row.id,
            row.channel_id,
            row.name,
            True,
            list(row.event_types),
            row.min_severity,
            deepcopy(row.filter),
            row.digest_seconds,
            row.template,
            row.created_by,
            self._now,
        )
        self.state.notification_rules[row.id] = rule
        self.state.notification_rule_spaces[row.id] = self.space.id
        return rule

    async def notification_rules(self) -> list[NotificationRuleRow]:
        return sorted(
            [
                row
                for row in self.state.notification_rules.values()
                if self.state.notification_rule_spaces.get(row.id) == self.space.id
            ],
            key=lambda row: (row.name, row.id),
        )

    async def notification_delivery_read_page(
        self, *, limit: int, before: tuple[datetime, int] | None = None
    ) -> list[NotificationDeliveryReadRow]:
        rows: list[NotificationDeliveryReadRow] = []
        for item in self.state.notifications.values():
            rule = self.state.notification_rules.get(item["rule_id"])
            channel = self.state.notification_channels.get(item["channel_id"])
            if (
                rule is None
                or channel is None
                or self.state.notification_rule_spaces.get(rule.id) != self.space.id
                or channel[2] != self.space.id
                or (before is not None and (item["scheduled_at"], item["id"]) >= before)
            ):
                continue
            rows.append(
                NotificationDeliveryReadRow(
                    item["id"],
                    rule.id,
                    rule.name,
                    channel[0].id,
                    channel[0].title,
                    list(item["event_ids"]),
                    item["status"],
                    item["attempt"],
                    item["scheduled_at"],
                    item.get("sent_at"),
                    item.get("error"),
                )
            )
        return sorted(rows, key=lambda row: (row.scheduled_at, row.id), reverse=True)[:limit]

    async def retry_notification_delivery(self, delivery_id: int) -> bool:
        row = self.state.notifications.get(delivery_id)
        if row is None:
            return False
        rule = self.state.notification_rules.get(row["rule_id"])
        if rule is None or self.state.notification_rule_spaces.get(rule.id) != self.space.id:
            return False
        row.update(
            status="pending",
            attempt=0,
            scheduled_at=self._now,
            sent_at=None,
            error=None,
        )
        return True

    async def update_notification_rule(self, rule_id: UUID, values: dict[str, Any]) -> bool:
        rule = self.state.notification_rules.get(rule_id)
        if rule is None or self.state.notification_rule_spaces.get(rule_id) != self.space.id:
            return False
        self.state.notification_rules[rule_id] = replace(rule, **values, updated_at=self._now)
        return True

    async def delete_notification_rule(self, rule_id: UUID) -> bool:
        if self.state.notification_rule_spaces.get(rule_id) != self.space.id:
            return False
        self.state.notification_rule_spaces.pop(rule_id, None)
        return self.state.notification_rules.pop(rule_id, None) is not None

    # --------------------------------------------------------- admin durable state (A4.1)

    async def get_import_run(self, run_id: UUID) -> ImportRunRow | None:
        row = self.state.import_runs.get(run_id)
        return deepcopy(row) if row is not None and row.space_id == self.space.id else None

    async def list_import_runs(self, *, limit: int = 100) -> list[ImportRunRow]:
        rows = [row for row in self.state.import_runs.values() if row.space_id == self.space.id]
        rows.sort(key=lambda row: (row.started_at, row.id), reverse=True)
        return deepcopy(rows[:limit])

    async def create_import_run(self, run: NewImportRun, items: Sequence[NewImportItem]) -> bool:
        if run.id in self.state.import_runs:
            return False
        self.state.import_runs[run.id] = ImportRunRow(
            id=run.id,
            space_id=self.space.id,
            service=run.service,
            actor=run.actor,
            source=run.source,
            state="pending",
            on_existing=run.on_existing,
            verify_sessions=run.verify_sessions,
            labels=dict(run.labels),
            started_at=self._now,
            updated_at=self._now,
            finished_at=None,
            cancel_requested_at=None,
            total=run.total,
            processed=0,
            created=0,
            updated=0,
            skipped=0,
            failed=0,
            report={},
        )
        for item in items:
            self.state.import_items[(run.id, item.position)] = {
                "space_id": self.space.id,
                "state": "pending",
                "claim_token": 0,
                "claimed_by": None,
                "claim_expires_at": None,
                "payload": item.payload,
            }
        return True

    async def active_import_runs(self, *, limit: int = 10) -> list[ImportRunRow]:
        rows = [
            row
            for row in self.state.import_runs.values()
            if row.space_id == self.space.id and row.state in {"pending", "running", "cancelling"}
        ]
        rows.sort(key=lambda row: (row.started_at, row.id))
        return deepcopy(rows[:limit])

    async def claim_import_items(
        self,
        run_id: UUID,
        *,
        worker: str,
        limit: int,
        claim_for: timedelta,
    ) -> list[ImportItemRow]:
        run = self.state.import_runs.get(run_id)
        if (
            run is None
            or run.space_id != self.space.id
            or run.cancel_requested_at is not None
            or run.state not in {"pending", "running"}
        ):
            return []
        for key, item in self.state.import_items.items():
            if (
                key[0] == run_id
                and item["state"] == "running"
                and item["claim_expires_at"] is not None
                and item["claim_expires_at"] <= self._now
            ):
                item.update(state="pending", claimed_by=None, claim_expires_at=None)
        positions = sorted(
            position
            for (item_run, position), item in self.state.import_items.items()
            if item_run == run_id and item["state"] == "pending"
        )[:limit]
        claimed: list[ImportItemRow] = []
        for position in positions:
            item = self.state.import_items[(run_id, position)]
            item["state"] = "running"
            item["claim_token"] += 1
            item["claimed_by"] = worker
            item["claim_expires_at"] = self._now + claim_for
            claimed.append(ImportItemRow(run_id, position, item["claim_token"], deepcopy(item["payload"])))
        if claimed:
            self.state.import_runs[run_id] = replace(run, state="running", updated_at=self._now)
        return claimed

    async def finish_import_items(
        self, run_id: UUID, results: Sequence[ImportItemResult]
    ) -> ImportRunRow | None:
        run = self.state.import_runs.get(run_id)
        if run is None or run.space_id != self.space.id:
            return None
        counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        report = deepcopy(run.report)
        report.setdefault("created_ids", [])
        report.setdefault("skipped", [])
        report.setdefault("failed", [])
        accepted = 0
        for result in results:
            item = self.state.import_items.get((run_id, result.position))
            if item is None or item["state"] != "running" or item["claim_token"] != result.claim_token:
                continue
            item.update(
                state=("succeeded" if result.outcome in {"created", "updated"} else result.outcome),
                claimed_by=None,
                claim_expires_at=None,
                error_code=result.error_code,
                error=result.error,
            )
            accepted += 1
            counts[result.outcome] += 1
            if result.outcome == "created" and result.identity_id is not None:
                report["created_ids"] = [*report["created_ids"][-4999:], str(result.identity_id)]
            elif result.outcome in {"skipped", "failed"}:
                report[result.outcome] = [
                    *report[result.outcome][-999:],
                    {
                        "position": result.position,
                        "external_ref": result.external_ref,
                        "code": result.error_code,
                        "reason": result.error,
                    },
                ]
        open_items = [
            item
            for (item_run, _), item in self.state.import_items.items()
            if item_run == run_id and item["state"] in {"pending", "running"}
        ]
        running = [item for item in open_items if item["state"] == "running"]
        unreadable_keys = {
            key
            for key, item in self.state.import_items.items()
            if key[0] == run_id and item.get("error_code") == UNREADABLE_IMPORT_ITEM
        }
        terminal = None
        if run.cancel_requested_at is not None and not running:
            terminal = "cancelled"
        elif not open_items:
            # нерасшифрованные записи — не «успех»: прогон failed, сами записи остаются (R2-S09)
            terminal = "failed" if unreadable_keys else "succeeded"
        row = replace(
            run,
            state=terminal or run.state,
            updated_at=self._now,
            finished_at=self._now if terminal else None,
            processed=run.processed + accepted,
            created=run.created + counts["created"],
            updated=run.updated + counts["updated"],
            skipped=run.skipped + counts["skipped"],
            failed=run.failed + counts["failed"],
            report=report,
        )
        self.state.import_runs[run_id] = row
        if terminal:
            keep = unreadable_keys if terminal == "failed" else set()
            for key in [key for key in self.state.import_items if key[0] == run_id and key not in keep]:
                del self.state.import_items[key]
        return deepcopy(row)

    async def request_import_cancel(self, run_id: UUID) -> ImportRunRow | None:
        run = self.state.import_runs.get(run_id)
        if run is None or run.space_id != self.space.id:
            return None
        if run.state in {"succeeded", "failed", "cancelled"}:
            return deepcopy(run)
        for (item_run, _), item in self.state.import_items.items():
            if (
                item_run == run_id
                and item["state"] == "running"
                and item["claim_expires_at"] is not None
                and item["claim_expires_at"] <= self._now
            ):
                item.update(state="pending", claimed_by=None, claim_expires_at=None)
        running = any(
            item["state"] == "running"
            for (item_run, _), item in self.state.import_items.items()
            if item_run == run_id
        )
        state = "cancelling" if running else "cancelled"
        row = replace(
            run,
            state=state,
            cancel_requested_at=self._now,
            updated_at=self._now,
            finished_at=self._now if state == "cancelled" else None,
        )
        self.state.import_runs[run_id] = row
        if state == "cancelled":
            for key in [key for key in self.state.import_items if key[0] == run_id]:
                del self.state.import_items[key]
        return deepcopy(row)

    async def enqueue_acceptance(self, identity_ids: Sequence[UUID], policy: dict[str, Any]) -> int:
        inserted = 0
        for identity_id in identity_ids:
            if self._own(identity_id) is None or identity_id in self.state.acceptance:
                continue
            self.state.acceptance[identity_id] = AcceptanceRow(
                identity_id=identity_id,
                space_id=self.space.id,
                policy=deepcopy(policy),
                attempts=0,
                created_at=self._now,
                updated_at=self._now,
            )
            inserted += 1
        return inserted

    async def list_acceptance(self, *, limit: int = 100) -> list[AcceptanceRow]:
        rows = [row for row in self.state.acceptance.values() if row.space_id == self.space.id]
        rows.sort(key=lambda row: (row.created_at, row.identity_id))
        return deepcopy(rows[:limit])

    async def update_acceptance(
        self, identity_id: UUID, *, attempts: int | None = None, done: bool = False
    ) -> bool:
        row = self.state.acceptance.get(identity_id)
        if row is None or row.space_id != self.space.id:
            return False
        if done:
            del self.state.acceptance[identity_id]
        else:
            self.state.acceptance[identity_id] = replace(
                row,
                attempts=row.attempts if attempts is None else attempts,
                updated_at=self._now,
            )
        return True

    async def enqueue_provider_operation(
        self,
        identity_id: UUID,
        kind: str,
        *,
        available_at: datetime | None = None,
        deadline_at: datetime | None = None,
    ) -> ProviderOperationRow:
        if self._own(identity_id) is None:
            raise NotFound("identity", identity_id)
        if any(
            op.identity_id == identity_id and op.kind == kind and op.state in ("pending", "running")
            for op in self.state.provider_operations.values()
        ):
            raise AlreadyExists("provider_operation", f"{identity_id}/{kind}")
        row = ProviderOperationRow(
            new_id(),
            self.space.id,
            identity_id,
            kind,
            "pending",
            0,
            None,
            0,
            None,
            None,
            available_at or self._now,
            deadline_at,
            {},
            None,
            self._now,
            self._now,
            None,
            1,
        )
        self.state.provider_operations[row.id] = row
        return deepcopy(row)

    async def claim_provider_operations(
        self,
        worker: str,
        *,
        limit: int,
        claim_for: timedelta,
        kinds: frozenset[str] | None = None,
        operation_ids: Sequence[UUID] | None = None,
    ) -> list[ProviderClaim]:
        due = sorted(
            (
                row
                for row in self.state.provider_operations.values()
                if row.space_id == self.space.id
                and row.state in CLAIMABLE_OPERATIONS
                and row.available_at <= self._now
                and (kinds is None or row.kind in kinds)
                and (operation_ids is None or row.id in operation_ids)
            ),
            key=lambda row: (row.available_at, row.id),
        )[:limit]
        claims: list[ProviderClaim] = []
        for row in due:
            claimed = replace(
                row,
                state="running",
                attempt=row.attempt + 1,
                claim_id=uuid4(),
                claim_token=row.claim_token + 1,
                claimed_by=worker,
                claim_expires_at=self._now + claim_for,
                updated_at=self._now,
                revision=row.revision + 1,
            )
            self.state.provider_operations[row.id] = claimed
            assert claimed.claim_id is not None and claimed.claim_expires_at is not None
            claims.append(
                ProviderClaim(
                    claimed.id,
                    claimed.identity_id,
                    claimed.kind,
                    claimed.attempt,
                    claimed.claim_id,
                    claimed.claim_token,
                    claimed.claim_expires_at,
                    claimed.deadline_at,
                )
            )
        return claims

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
        state, available_at = resolve_outcome(
            outcome, attempt=claim.attempt, deadline_at=claim.deadline_at, now=self._now, policy=policy
        )
        if fence is not None:
            await self.check_fence(fence)
        row = self.state.provider_operations.get(claim.operation_id)
        if (
            row is None
            or row.space_id != self.space.id
            or row.claim_id != claim.claim_id
            or row.claim_token != claim.claim_token
            or row.state != "running"
            or row.claim_expires_at is None
            or row.claim_expires_at <= self._now
        ):
            return False
        self.state.provider_operations[row.id] = replace(
            row,
            state=state,
            error_code=error_code,
            claim_expires_at=None,
            result=deepcopy(result) if result is not None else row.result,
            available_at=available_at or row.available_at,
            finished_at=self._now if state in TERMINAL_OPERATIONS else None,
            updated_at=self._now,
            revision=row.revision + 1,
        )
        return True

    async def recover_provider_operations(self, *, limit: int, policy: Any = None) -> int:
        stale = sorted(
            (
                row
                for row in self.state.provider_operations.values()
                if row.space_id == self.space.id
                and row.state == "running"
                and row.claim_expires_at is not None
                and row.claim_expires_at <= self._now
            ),
            key=lambda row: row.claim_expires_at or self._now,
        )[:limit]
        for row in stale:
            state, available_at = resolve_recovery(
                attempt=row.attempt, deadline_at=row.deadline_at, now=self._now, policy=policy
            )
            self.state.provider_operations[row.id] = replace(
                row,
                state=state,
                error_code="claim_expired",
                claim_expires_at=None,
                available_at=available_at or row.available_at,
                finished_at=self._now if state in TERMINAL_OPERATIONS else None,
                updated_at=self._now,
                revision=row.revision + 1,
            )
        return len(stale)

    async def cancel_provider_operation(self, operation_id: UUID) -> bool:
        row = self.state.provider_operations.get(operation_id)
        if row is None or row.space_id != self.space.id or row.state not in CLAIMABLE_OPERATIONS:
            return False
        self.state.provider_operations[operation_id] = replace(
            row, state="cancelled", finished_at=self._now, updated_at=self._now, revision=row.revision + 1
        )
        return True

    async def list_provider_operations(
        self,
        *,
        identity_id: UUID | None = None,
        states: frozenset[str] | None = None,
        limit: int = 100,
    ) -> list[ProviderOperationRow]:
        rows = [
            row
            for row in self.state.provider_operations.values()
            if row.space_id == self.space.id
            and (identity_id is None or row.identity_id == identity_id)
            and (states is None or row.state in states)
        ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return deepcopy(rows[:limit])

    async def due_identity_timers(self, limit: int) -> list[tuple[UUID, str]]:
        due: list[tuple[datetime, UUID, str]] = []
        for row in self.state.identities.values():
            if row.space_id != self.space.id:
                continue
            # только состояния с переходом по таймеру, как в PostgreSQL (R2-F06)
            region = self.state.regions.get((row.id, "standing", ""))
            pinned = (
                region is not None and region.pinned_until is not None and region.pinned_until > self._now
            )
            if (
                row.standing in STANDING.on_expire
                and row.standing_until is not None
                and row.standing_until <= self._now
                and not pinned
            ):
                due.append((row.standing_until, row.id, "standing"))
            if (
                row.availability in AVAILABILITY.on_expire
                and row.availability_until is not None
                and row.availability_until <= self._now
            ):
                due.append((row.availability_until, row.id, "availability"))
        due.sort()
        return [(identity_id, region) for _, identity_id, region in due[:limit]]

    async def stuck_logins(self, limit: int) -> list[tuple[UUID, datetime]]:
        stuck: list[tuple[UUID, datetime]] = []
        for row in self.state.identities.values():
            if row.space_id != self.space.id or row.auth_state != "logging_in":
                continue
            lease = self.state.leases.get(row.id)
            if lease is not None and lease.expires_at > self._now:
                continue
            region = self.state.regions.get((row.id, "auth", "default"))
            stuck.append((row.id, region.changed_at if region is not None else row.updated_at))
        return sorted(stuck, key=lambda item: (item[1], item[0]))[:limit]

    async def expire_leases(self, limit: int) -> int:
        expired = sorted(
            [
                lease
                for lease in self.state.leases.values()
                if self._own(lease.identity_id) and lease.expires_at <= self._now
            ],
            key=lambda row: row.expires_at,
        )[:limit]
        drafts: list[EventDraft] = []
        for lease in expired:
            self.state.leases.pop(lease.identity_id, None)
            history = self.state.history[lease.lease_id]
            if history.released_at is None:
                self.state.history[lease.lease_id] = replace(
                    history,
                    released_at=lease.expires_at,
                    duration=lease.expires_at - history.acquired_at,
                    end_reason=str(expiry_reason(history.hold_deadline, lease.expires_at)),
                )
                identity = self.state.identities[lease.identity_id]
                drafts.append(
                    EventDraft(
                        "lease.expired",
                        "maintenance",
                        service=identity.service,
                        identity_id=identity.id,
                        lease_id=lease.lease_id,
                        data={
                            "pool": lease.pool,
                            "holder": lease.holder,
                            "fencing": lease.fencing,
                            "reason": str(expiry_reason(history.hold_deadline, lease.expires_at)),
                            "expired_at": lease.expires_at.isoformat(),
                        },
                    )
                )
        await self.emit(drafts)
        return len(drafts)


class MemoryStore(Store):
    """Полный Store без внешней инфраструктуры, предназначенный для тестов и локальных примеров."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._own_clock = clock is not None
        self.clock = clock or SystemClock()
        self._state = _MemoryState()
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._consumer_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._listeners: defaultdict[str, set[asyncio.Event]] = defaultdict(set)

    @property
    def _lock(self) -> asyncio.Lock:
        """Блокировка текущего event loop: хранилище живёт дольше одного `asyncio.run()` (R2-P19)."""
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            # блокировки закрытых loop больше не нужны: их ожидающие не вернутся
            self._locks = {known: item for known, item in self._locks.items() if not known.is_closed()}
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    def bind_clock(self, clock: Clock | None) -> Clock:
        """Время записей хранилища и дедлайны хаба идут по одним часам (R2-P21).

        Хранилище без своих часов принимает часы хаба; хаб без часов принимает часы хранилища;
        двое разных часов — ошибка конфигурации: под `FakeClock` одна половина жила бы в
        управляемом времени, другая — в реальном.
        """
        if clock is None:
            return self.clock
        if self._own_clock and clock is not self.clock:
            raise ValidationFailed("MemoryStore и IdentityHub получили разные часы: передайте один clock")
        self.clock = clock
        self._own_clock = True
        return clock

    @asynccontextmanager
    async def transaction(self, space: SpaceRef, *, session: Any | None = None) -> AsyncIterator[MemoryTx]:
        if session is not None:
            raise TypeError("MemoryStore не поддерживает внешнюю SQL-сессию")
        async with self._lock:
            snapshot = _snapshot(self._state)
            tx = MemoryTx(self, space, self.clock.now())
            try:
                yield tx
            except BaseException:
                _restore(self._state, snapshot)
                raise
            else:
                if tx._notify:
                    for event in self._listeners.get("ih_events", ()):
                        event.set()

    async def current_time(self) -> datetime:
        return self.clock.now()

    async def schema_revisions(self) -> tuple[str, list[str]]:
        """MemoryStore всегда совместим: схема живёт в коде."""
        return MEMORY_SCHEMA_REVISION, [MEMORY_SCHEMA_REVISION]

    async def sync_services(self, services: Sequence[ServiceRow]) -> None:
        async with self._lock:
            self._state.services = {service.name: deepcopy(service) for service in services}
            for identity_id, identity in self._state.identities.items():
                service = self._state.services.get(identity.service)
                if service is None:
                    continue
                factors = frozenset(identity.verified_factors)
                rank = max(
                    (
                        index
                        for index, required in enumerate(service.verification_tiers)
                        if required <= factors
                    ),
                    default=None,
                )
                self._state.identities[identity_id] = replace(identity, verification_tier_rank=rank)

    async def ensure_space(
        self,
        slug: str,
        title: str | None = None,
        space_id: UUID | None = None,
        *,
        timezone: str = "UTC",
    ) -> SpaceRef:
        async with self._lock:
            existing = self._state.spaces.get(slug)
            if existing is not None:
                if space_id is not None and existing.id != space_id:
                    raise SpaceMismatch(f"пространство {slug!r} уже существует с другим id")
                return existing
            space = SpaceRef(space_id or uuid4(), slug, title or slug, timezone)
            self._state.spaces[slug] = space
            return space

    async def get_space(self, slug: str) -> SpaceRef | None:
        async with self._lock:
            return self._state.spaces.get(slug)

    async def list_spaces(self, slugs: frozenset[str] | None = None) -> list[SpaceRef]:
        async with self._lock:
            return [
                deepcopy(space)
                for slug, space in sorted(self._state.spaces.items())
                if slugs is None or slug in slugs
            ]

    async def process_events(
        self,
        consumer: str,
        handler: Callable[[list[Event]], Awaitable[int | None]],
        *,
        limit: int,
        external_only: bool = True,
        start_now: bool = False,
    ) -> int | None:
        consumer_lock = self._consumer_locks[consumer]
        if consumer_lock.locked():
            return None
        async with consumer_lock:
            async with self._lock:
                created = consumer not in self._state.cursors
                if start_now and created:
                    self._state.cursors[consumer] = max((e.id for e, _, _ in self._state.events), default=0)
                cursor = self._state.cursors.get(consumer, 0)
                oldest = next(
                    (event for event, _, external in self._state.events if external or not external_only),
                    None,
                )
                if not created and cursor != 0 and oldest is not None and cursor < oldest.id:
                    raise ConsumerCursorExpired(consumer, str(cursor), str(oldest.id))
                events = [
                    event
                    for event, _, external in self._state.events
                    if (external or not external_only) and event.id > cursor
                ][:limit]
            if not events:
                return 0
            done = await handler(events)
            handled = len(events) if done is None else max(0, min(done, len(events)))
            if handled == 0:
                return 0
            async with self._lock:
                self._state.cursors[consumer] = max(
                    self._state.cursors.get(consumer, 0), events[handled - 1].id
                )
            return handled

    async def rollup_source_events(self, space_slug: str, start: datetime, end: datetime) -> list[Event]:
        async with self._lock:
            return [
                deepcopy(event)
                for event, _, _ in self._state.events
                if event.space == space_slug and start <= event.occurred_at < end
            ]

    async def replace_rollups(self, rows: Sequence[RollupRow]) -> None:
        async with self._lock:
            for row in rows:
                self._state.rollups[row.key] = row.value

    async def apply_metric_rollup_batch(self, batch_id: UUID, rows: Sequence[RollupDelta]) -> bool:
        async with self._lock:
            if batch_id in self._state.metric_batches:
                return False
            self._state.metric_batches[batch_id] = self.clock.now()
            for delta in rows:
                row = delta.row
                if delta.additive:
                    self._state.rollups[row.key] = self._state.rollups.get(row.key, 0) + row.value
                else:
                    self._state.rollups[row.key] = row.value
            return True

    async def event_rollup_rows(
        self, space_id: UUID, bucket_size: BucketSize, start: datetime, end: datetime
    ) -> list[RollupRow]:
        async with self._lock:
            return [
                RollupRow(key, value)
                for key, value in self._state.rollups.items()
                if key.space_id == space_id
                and key.bucket_size is bucket_size
                and start <= key.bucket_start < end
                and not key.metric.startswith("telemetry.")
            ]

    async def clear_rollups(self, space_id: UUID, start: datetime, end: datetime) -> int:
        async with self._lock:
            keys = [
                key
                for key in self._state.rollups
                if key.space_id == space_id
                and start <= key.bucket_start < end
                and not key.metric.startswith("telemetry.")  # как в PostgreSQL: телеметрия остаётся
            ]
            for key in keys:
                del self._state.rollups[key]
            return len(keys)

    async def purge_minute_rollups(self, before: datetime) -> int:
        async with self._lock:
            keys = [
                key
                for key in self._state.rollups
                if key.bucket_size is BucketSize.MINUTE and key.bucket_start < before
            ]
            for key in keys:
                del self._state.rollups[key]
            return len(keys)

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
    ) -> list[RollupRow]:
        async with self._lock:
            space = self._state.spaces.get(space_slug)
            if space is None:
                return []
            return sorted(
                (
                    RollupRow(key, value)
                    for key, value in self._state.rollups.items()
                    if key.space_id == space.id
                    and key.metric == metric
                    and key.bucket_size is bucket_size
                    and start <= key.bucket_start < end
                    and (service is None or key.service == service)
                    and (dims is None or key.dims == dims)
                ),
                key=lambda row: (row.key.bucket_start, row.key.service, row.key.dims),
            )

    async def capture_state_snapshot(self, taken_at: datetime, *, source: str) -> int:
        async with self._lock:
            space_ids = {space.id for space in self._state.spaces.values()}
            for space_id in space_ids:
                self._state.snapshot_coverage[space_id, taken_at] = (
                    self.clock.now(),
                    source,
                    True,
                )
            for key in [key for key in self._state.state_snapshots if key[-1] == taken_at]:
                del self._state.state_snapshots[key]
            counts: defaultdict[tuple[UUID, str, str, str, datetime], int] = defaultdict(int)
            for (identity_id, region, region_key), row in self._state.regions.items():
                identity = self._state.identities.get(identity_id)
                if identity is None:
                    continue
                name = f"{region}:{region_key}" if region_key else region
                counts[identity.space_id, identity.service, name, row.state, taken_at] += 1
            self._state.state_snapshots.update(counts)
            return len(counts)

    async def read_state_snapshots(
        self, space_slug: str, start: datetime, end: datetime
    ) -> list[StateSnapshotFrame]:
        async with self._lock:
            space = self._state.spaces.get(space_slug)
            if space is None:
                return []
            frames: list[StateSnapshotFrame] = []
            for (space_id, taken_at), (captured_at, source, complete) in sorted(
                self._state.snapshot_coverage.items(), key=lambda item: item[0][1]
            ):
                if space_id != space.id or not (start <= taken_at < end):
                    continue
                rows = tuple(
                    StateSnapshotRow(space_id, service, region, state, at, count)
                    for (row_space, service, region, state, at), count in sorted(
                        self._state.state_snapshots.items(), key=lambda item: item[0][1:]
                    )
                    if row_space == space_id and at == taken_at
                )
                frames.append(StateSnapshotFrame(taken_at, captured_at, source, complete, rows))
            return frames

    async def purge_state_snapshots(self, before: datetime) -> int:
        async with self._lock:
            coverage = [key for key in self._state.snapshot_coverage if key[1] < before]
            for coverage_key in coverage:
                del self._state.snapshot_coverage[coverage_key]
            rows = [key for key in self._state.state_snapshots if key[-1] < before]
            for row_key in rows:
                del self._state.state_snapshots[row_key]
            return len(coverage)

    @asynccontextmanager
    async def listen(self, channel: str) -> AsyncIterator[asyncio.Event | None]:
        event = asyncio.Event()
        self._listeners[channel].add(event)
        try:
            yield event
        finally:
            self._listeners[channel].discard(event)

    async def enabled_webhook_endpoints(self) -> list[WebhookEndpointRow]:
        async with self._lock:
            return [row for row in self._state.webhook_endpoints.values() if row.enabled]

    async def identity_labels(self, identity_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
        wanted = set(identity_ids)
        async with self._lock:
            return {
                row.id: deepcopy(row.labels) for row in self._state.identities.values() if row.id in wanted
            }

    async def enqueue_webhook_deliveries(self, deliveries: Sequence[DeliveryEnqueue]) -> int:
        async with self._lock:
            existing = {(row.endpoint_id, row.event_id) for row in self._state.webhook_deliveries.values()}
            inserted = 0
            now = self.clock.now()
            for item in deliveries:
                key = (item.endpoint_id, item.event_id)
                if key in existing or item.endpoint_id not in self._state.webhook_endpoints:
                    continue
                delivery_id = self._state.next_webhook_delivery_id
                self._state.next_webhook_delivery_id += 1
                self._state.webhook_deliveries[delivery_id] = WebhookDeliveryRow(
                    delivery_id,
                    item.endpoint_id,
                    item.event_id,
                    "pending",
                    0,
                    now,
                    None,
                    None,
                    None,
                    now,
                    None,
                    item.resource_key,
                    item.resource_revision,
                )
                existing.add(key)
                inserted += 1
            return inserted

    def _blocked_by_earlier_delivery(self, row: WebhookDeliveryRow) -> bool:
        """Head-of-line для `ordered`-получателя (`IMP-21`, `D-328`); вызывается под `self._lock`."""
        endpoint = self._state.webhook_endpoints.get(row.endpoint_id)
        if endpoint is None or not endpoint.ordered or row.resource_key is None:
            return False
        order = (row.resource_revision, row.id)
        return any(
            other.endpoint_id == row.endpoint_id
            and other.resource_key == row.resource_key
            and other.status != "succeeded"
            and other.id != row.id
            and (other.resource_revision, other.id) < order
            for other in self._state.webhook_deliveries.values()
        )

    async def claim_webhook_deliveries(self, *, limit: int, claim_for: timedelta) -> list[WebhookAttempt]:
        async with self._lock:
            now = self.clock.now()
            events = {event.event_id: event for event, _, _ in self._state.events}
            endpoints = self._state.webhook_endpoints
            # фильтр отключённых endpoint — до limit: их доставки не заслоняют живые (R2-P17)
            rows = sorted(
                [
                    row
                    for row in self._state.webhook_deliveries.values()
                    if row.status in {"pending", "failed"}
                    and row.next_attempt_at <= now
                    and row.endpoint_id in endpoints
                    and endpoints[row.endpoint_id].enabled
                    and row.event_id in events
                    and not self._blocked_by_earlier_delivery(row)
                ],
                key=lambda row: (row.next_attempt_at, row.id),
            )[:limit]
            attempts: list[WebhookAttempt] = []
            for row in rows:
                endpoint = endpoints[row.endpoint_id]
                event = events[row.event_id]
                attempt = row.attempt + 1
                self._state.webhook_deliveries[row.id] = replace(
                    row, attempt=attempt, next_attempt_at=now + claim_for
                )
                attempts.append(
                    WebhookAttempt(row.id, attempt, endpoint.id, endpoint.url, endpoint.secret, event)
                )
            return attempts

    async def release_webhook_deliveries(
        self, claims: Sequence[tuple[int, int]], *, retry_after: timedelta
    ) -> int:
        async with self._lock:
            now = self.clock.now()
            released = 0
            for delivery_id, attempt in claims:
                row = self._state.webhook_deliveries.get(delivery_id)
                if (
                    row is None
                    or row.status not in {"pending", "failed", "paused"}
                    or row.attempt != attempt
                    or attempt < 1
                ):
                    continue
                self._state.webhook_deliveries[delivery_id] = replace(
                    row, attempt=attempt - 1, next_attempt_at=now + retry_after
                )
                released += 1
            return released

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
        async with self._lock:
            row = self._state.webhook_deliveries.get(delivery_id)
            if row is None or row.status not in {"pending", "failed", "paused"} or row.attempt != attempt:
                return False
            now = self.clock.now()
            endpoint = self._state.webhook_endpoints[row.endpoint_id]
            failure_streak = 0 if succeeded else endpoint.failure_streak + 1
            exhausted = not succeeded and failure_streak >= max_failures
            # endpoint, уже отключённый вручную, не отключается повторно: его причина сохраняется
            disabled = exhausted and endpoint.enabled
            self._state.webhook_endpoints[endpoint.id] = replace(
                endpoint,
                enabled=endpoint.enabled and not disabled,
                disabled_reason="consecutive_failures" if disabled else endpoint.disabled_reason,
                failure_streak=failure_streak,
            )
            if succeeded:
                status = "succeeded"
            elif exhausted:
                status = "dead"
            else:  # endpoint отключён после захвата: повтор — после включения
                status = "paused" if row.status == "paused" else "failed"
            self._state.webhook_deliveries[delivery_id] = replace(
                row,
                status=status,
                next_attempt_at=now + retry_after,
                response_code=response_code,
                response_ms=response_ms,
                error=error,
                delivered_at=now if succeeded else None,
            )
            if disabled:
                _pause_deliveries(self._state, endpoint.id)
                space = next(item for item in self._state.spaces.values() if item.id == endpoint.space_id)
                event = Event(
                    self._state.next_event_id,
                    uuid4(),
                    "webhook.endpoint.disabled",
                    Severity.CRITICAL,
                    now,
                    space.slug,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "system:webhooks",
                    {"endpoint_id": str(endpoint.id), "failure_streak": failure_streak},
                )
                self._state.next_event_id += 1
                self._state.events.append((event, endpoint.space_id, True))
                for signal in self._listeners.get("ih_events", ()):
                    signal.set()
            return True

    async def enabled_notification_targets(self) -> list[NotificationTargetRow]:
        async with self._lock:
            spaces = {space.id: space.slug for space in self._state.spaces.values()}
            targets = []
            for rule in self._state.notification_rules.values():
                item = self._state.notification_channels.get(rule.channel_id)
                if (
                    not rule.enabled
                    or item is None
                    or not item[0].enabled
                    or self._state.notification_rule_spaces.get(rule.id) != item[2]
                ):
                    continue
                channel, secret, space_id = item
                slug = spaces[space_id]
                targets.append(NotificationTargetRow(rule, slug, deepcopy(channel.config), secret))
            return targets

    async def enqueue_notifications(self, items: Sequence[NotificationEnqueue]) -> int:
        async with self._lock:
            inserted = 0
            # как в PostgreSQL: без дайджеста — уникальность (правило, событие); в дайджесте —
            # событие не добавляется дважды в открытое окно
            existing = {
                (row["rule_id"], row["event_ids"][0])
                for row in self._state.notifications.values()
                if row["digest_key"] is None
            }
            for item in items:
                key = (item.rule_id, item.event_id)
                if item.digest_key is None and key in existing:
                    continue
                if item.digest_key is not None:
                    digest = next(
                        (
                            row
                            for row in self._state.notifications.values()
                            if row["digest_key"] == item.digest_key and row["status"] == "pending"
                        ),
                        None,
                    )
                    if digest is not None:
                        if item.event_id not in digest["event_ids"]:
                            digest["event_ids"].append(item.event_id)
                        inserted += 1
                        continue
                notification_id = self._state.next_notification_id
                self._state.next_notification_id += 1
                self._state.notifications[notification_id] = {
                    "id": notification_id,
                    "rule_id": item.rule_id,
                    "channel_id": item.channel_id,
                    "event_ids": [item.event_id],
                    "digest_key": item.digest_key,
                    "scheduled_at": item.scheduled_at,
                    "status": "pending",
                    "attempt": 0,
                    "error": None,
                }
                existing.add(key)
                inserted += 1
            return inserted

    async def claim_notifications(self, *, limit: int, claim_for: timedelta) -> list[NotificationAttempt]:
        async with self._lock:
            now = self.clock.now()

            def claimable(row: dict[str, Any]) -> bool:
                rule = self._state.notification_rules.get(row["rule_id"])
                channel_item = self._state.notification_channels.get(row["channel_id"])
                paused_until = self._state.channel_pauses.get(row["channel_id"])
                return (
                    rule is not None
                    and channel_item is not None
                    and rule.enabled
                    and channel_item[0].enabled
                    and (paused_until is None or paused_until <= now)
                )

            # фильтр отключённых и приостановленных каналов — до limit, как в PostgreSQL
            due = sorted(
                [
                    row
                    for row in self._state.notifications.values()
                    if row["status"] in {"pending", "failed"}
                    and row["scheduled_at"] <= now
                    and claimable(row)
                ],
                key=lambda row: (row["scheduled_at"], row["id"]),
            )[:limit]
            events = {event.event_id: event for event, _, _ in self._state.events}
            attempts = []
            for row in due:
                rule = self._state.notification_rules[row["rule_id"]]
                channel_item = self._state.notification_channels[row["channel_id"]]
                row["status"] = "failed"
                row["attempt"] += 1
                row["scheduled_at"] = now + claim_for
                channel, secret, _ = channel_item
                attempts.append(
                    NotificationAttempt(
                        row["id"],
                        row["attempt"],
                        rule.id,
                        channel.id,
                        rule.template,
                        deepcopy(channel.config),
                        secret,
                        tuple(events[event_id] for event_id in row["event_ids"] if event_id in events),
                    )
                )
            return attempts

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
        async with self._lock:
            row = self._state.notifications.get(notification_id)
            if row is None or row["status"] not in {"pending", "failed"} or row["attempt"] != attempt:
                return False
            now = self.clock.now()
            if succeeded:
                row.update(status="succeeded", sent_at=now, error=None)
            elif pause_channel is not None:
                # 429: попытка не засчитывается, канал ждёт до конца паузы (R2-F03)
                row.update(attempt=attempt - 1, scheduled_at=now + pause_channel, error=error)
                until = now + pause_channel
                current = self._state.channel_pauses.get(row["channel_id"])
                self._state.channel_pauses[row["channel_id"]] = max(current, until) if current else until
            else:
                row.update(
                    status="dead" if attempt >= max_attempts else "failed",
                    scheduled_at=now + retry_after,
                    error=error,
                )
            return True

    async def maintenance_spaces(self) -> list[SpaceRef]:
        async with self._lock:
            return sorted(self._state.spaces.values(), key=lambda row: row.slug)

    async def cleanup_maintenance(
        self, retention: Mapping[str, timedelta], names: frozenset[str], *, batch_size: int = 1000
    ) -> dict[str, int]:
        async with self._lock:  # в памяти транзакций нет: `batch_size` на результат не влияет
            now = self.clock.now()
            result: dict[str, int] = {}
            never = datetime.max.replace(tzinfo=UTC)  # незавершённое не удаляется, как NULL в SQL
            if "quota_windows" in names:
                keys = [key for key in self._state.quotas if key[3] + timedelta(seconds=key[2]) <= now]
                for key in keys:
                    del self._state.quotas[key]
                result["quota_windows"] = len(keys)
            if "operation_keys" in names:
                expired = [slot for slot, row in self._state.operation_keys.items() if row.expires_at <= now]
                for slot in expired:
                    del self._state.operation_keys[slot]
                result["operation_keys"] = len(expired)
            cleanups: dict[str, tuple[Any, Callable[[Any], datetime]]] = {
                "lease_logs": (self._state.lease_logs, lambda item: item["ts"]),
                "lease_history": (self._state.history, lambda item: item.released_at or never),
                "events": (self._state.events, lambda item: item[0].occurred_at),
                "webhook_deliveries": (self._state.webhook_deliveries, lambda item: item.created_at),
                "notifications": (self._state.notifications, lambda item: item["scheduled_at"]),
                "import_runs": (
                    self._state.import_runs,
                    lambda item: item.finished_at or never,
                ),
                "provider_operations": (
                    self._state.provider_operations,
                    lambda item: item.finished_at or never,
                ),
                "metric_batches": (self._state.metric_batches, lambda item: item),
                "closed_challenges": (self._state.challenges, lambda item: item.resolved_at or never),
            }
            # порядок фиксирован: доставки удалённых событий прибавляются к счёту `webhook_deliveries`
            for name in [name for name in cleanups if name in names]:
                collection, timestamp = cleanups[name]
                retention_key = "deliveries" if name in {"webhook_deliveries", "notifications"} else name
                cutoff = now - retention[retention_key]
                if isinstance(collection, list):
                    old_count = len(collection)
                    cursor_floor = min(self._state.cursors.values()) if self._state.cursors else None
                    cursor_holders = (
                        tuple(
                            sorted(
                                consumer
                                for consumer, cursor in self._state.cursors.items()
                                if cursor == cursor_floor
                            )
                        )
                        if cursor_floor is not None
                        else ()
                    )
                    gone = (
                        {
                            item[0].event_id
                            for item in collection
                            if timestamp(item) < cutoff
                            and (cursor_floor is None or item[0].id < cursor_floor)
                        }
                        if name == "events"
                        else set()
                    )
                    collection[:] = [
                        item
                        for item in collection
                        if timestamp(item) >= cutoff
                        or (name == "events" and cursor_floor is not None and item[0].id >= cursor_floor)
                    ]
                    result[name] = old_count - len(collection)
                    if name == "events":
                        retained = sum(
                            timestamp(item) < cutoff and item[0].id >= cursor_floor
                            for item in collection
                            if cursor_floor is not None
                        )
                        if retained:
                            result["events_retained"] = retained
                            for consumer in cursor_holders:
                                result[f"events_held_by:{consumer}"] = retained
                        orphaned = [
                            delivery_id
                            for delivery_id, row in self._state.webhook_deliveries.items()
                            if row.event_id in gone
                        ]
                        for delivery_id in orphaned:
                            del self._state.webhook_deliveries[delivery_id]
                        result["webhook_deliveries"] = result.get("webhook_deliveries", 0) + len(orphaned)
                else:
                    keys = [key for key, item in collection.items() if timestamp(item) < cutoff]
                    for key in keys:
                        del collection[key]
                    if name == "import_runs":
                        for item_key in [
                            item_key for item_key in self._state.import_items if item_key[0] in keys
                        ]:
                            del self._state.import_items[item_key]
                    if name == "lease_history":
                        for lease_id in [k for k in self._state.history_spaces if k not in collection]:
                            del self._state.history_spaces[lease_id]
                    result[name] = len(keys)
            for name, size in (("rollup_hours", BucketSize.HOUR), ("rollup_days", BucketSize.DAY)):
                if name not in names:
                    continue
                cutoff = now - retention[name]
                rollup_keys = [
                    key
                    for key in self._state.rollups
                    if key.bucket_size == size and key.bucket_start < cutoff
                ]
                for rollup_key in rollup_keys:
                    del self._state.rollups[rollup_key]
                result[name] = len(rollup_keys)
            return result

    async def stale_proxy_fingerprints(
        self, active_key_id: str, limit: int, after: UUID | None
    ) -> list[tuple[str, ProxyRow]]:
        async with self._lock:
            spaces = {space.id: space.slug for space in self._state.spaces.values()}
            rows = sorted(
                (
                    row
                    for row in self._state.proxies.values()
                    if row.fingerprint_key_id != active_key_id and (after is None or row.id > after)
                ),
                key=lambda row: row.id,
            )
            return [(spaces[row.space_id], row) for row in rows[:limit]]

    async def renew_leases(self, tokens: Sequence[UUID], ttl_seconds: float) -> dict[UUID, LeaseRenewal]:
        async with self._lock:
            now = self.clock.now()
            wanted = set(tokens)
            renewed: dict[UUID, LeaseRenewal] = {}
            for identity_id, lease in list(self._state.leases.items()):
                if lease.token in wanted and lease.expires_at > now:
                    expires_at = max(lease.expires_at, now + timedelta(seconds=ttl_seconds))
                    if lease.hold_deadline is not None:
                        expires_at = min(expires_at, lease.hold_deadline)
                    self._state.leases[identity_id] = replace(lease, expires_at=expires_at, heartbeat_at=now)
                    renewed[lease.token] = LeaseRenewal(
                        expires_at, yield_requested=lease.yield_requested_at is not None
                    )
            for token in wanted - set(renewed):
                renewed[token] = _gone(self._state, token)
            return renewed

    async def stale_normalizations(self, service: str, version: int, limit: int) -> list[tuple[UUID, str]]:
        async with self._lock:
            rows = sorted(
                (row for row in self._state.identities.values() if row.service == service),
                key=lambda row: row.id,
            )
            return [(row.id, row.external_ref) for row in rows if row.normalize_version != version][:limit]

    async def apply_normalizations(
        self, service: str, version: int, updates: Sequence[tuple[UUID, str]]
    ) -> list[list[UUID]]:
        async with self._lock:
            touched = {identity_id for identity_id, _ref in updates}
            others = [
                (row.id, row.normalized_ref)
                for row in self._state.identities.values()
                if row.service == service and row.id not in touched
            ]
            groups: dict[str, list[UUID]] = {}
            for identity_id, ref in [*others, *updates]:
                groups.setdefault(ref, []).append(identity_id)
            collisions = [sorted(ids) for _ref, ids in sorted(groups.items()) if len(ids) > 1]
            if collisions:
                return collisions
            for identity_id, ref in updates:
                row = self._state.identities[identity_id]
                self._state.identities[identity_id] = replace(
                    row, normalized_ref=ref, normalize_version=version
                )
            return []

    async def doctor(self, *, fix: bool, event_retention: timedelta | None = None) -> list[DoctorIssue]:
        async with self._lock:
            issues: list[DoctorIssue] = []
            spaces = {space.id: space.slug for space in self._state.spaces.values()}
            oldest = self._state.events[0][0] if self._state.events else None
            if oldest is not None:
                horizon_warning_at = (
                    self.clock.now() - event_retention * 0.9 if event_retention is not None else None
                )
                for consumer, cursor in sorted(self._state.cursors.items()):
                    if cursor == 0 or cursor > oldest.id:
                        continue
                    if cursor < oldest.id:
                        kind = "consumer_cursor_expired"
                    elif horizon_warning_at is not None and oldest.occurred_at <= horizon_warning_at:
                        kind = "consumer_cursor_at_retention_horizon"
                    else:
                        continue
                    issues.append(DoctorIssue(kind, "-", consumer, f">={oldest.id}", str(cursor)))
            event_seq: dict[UUID, int] = defaultdict(int)
            event_revision: dict[UUID, int] = defaultdict(int)
            for event, _, _ in self._state.events:
                if event.identity_id is not None and event.sequence is not None:
                    event_seq[event.identity_id] = max(event_seq[event.identity_id], event.sequence)
                    event_revision[event.identity_id] = max(
                        event_revision[event.identity_id], event.revision or 0
                    )
            for identity_id, row in list(self._state.identities.items()):
                changes: dict[str, Any] = {}
                standing = self._state.regions.get((identity_id, "standing", ""))
                if standing is not None:
                    expected = standing.state, standing.code, standing.until
                    actual = row.standing, row.standing_code, row.standing_until
                    if actual != expected:
                        issues.append(
                            DoctorIssue("standing", spaces[row.space_id], identity_id, expected, actual)
                        )
                        changes |= {
                            "standing": standing.state,
                            "standing_code": standing.code,
                            "standing_until": standing.until,
                        }
                auth = self._state.regions.get((identity_id, "auth", "default"))
                if auth is not None and row.auth_state != auth.state:
                    issues.append(
                        DoctorIssue(
                            "auth_state", spaces[row.space_id], identity_id, auth.state, row.auth_state
                        )
                    )
                    changes["auth_state"] = auth.state
                verified = sorted(
                    key
                    for (owner, region, key), state in self._state.regions.items()
                    if owner == identity_id and region == "verification" and state.state == "verified"
                )
                if sorted(row.verified_factors) != verified:
                    issues.append(
                        DoctorIssue(
                            "verified_factors",
                            spaces[row.space_id],
                            identity_id,
                            verified,
                            list(row.verified_factors),
                        )
                    )
                    changes["verified_factors"] = verified
                expected_seq = event_seq.get(identity_id, 0)
                if row.event_seq < expected_seq:
                    issues.append(
                        DoctorIssue(
                            "event_seq", spaces[row.space_id], identity_id, expected_seq, row.event_seq
                        )
                    )
                    changes["event_seq"] = expected_seq
                expected_revision = event_revision.get(identity_id, 0)
                if row.revision < expected_revision:
                    issues.append(
                        DoctorIssue(
                            "revision", spaces[row.space_id], identity_id, expected_revision, row.revision
                        )
                    )
                    changes["revision"] = expected_revision
                if fix and changes:
                    self._state.identities[identity_id] = replace(row, **changes)
            for proxy_id, proxy in self._state.proxies.items():
                expected_load = sum(bound == proxy_id for bound in self._state.bindings.values())
                actual_load = self._state.proxy_load.get(proxy_id, 0)
                if actual_load != expected_load:
                    issues.append(
                        DoctorIssue(
                            "proxy.bound_count",
                            spaces[proxy.space_id],
                            proxy_id,
                            expected_load,
                            actual_load,
                        )
                    )
                    if fix:
                        self._state.proxy_load[proxy_id] = expected_load
            return issues

    def _rotation_views(self) -> dict[str, tuple[Any, Callable[[Any], bytes]]]:
        state = self._state
        return {
            "credentials": (
                _PayloadView(state.credentials, 0),
                lambda key: credential_associated_data(key[0], key[1], key[2]),
            ),
            "sessions": (
                _AttributePayloadView(state.sessions, "payload"),
                lambda key: associated_data(
                    "ih_sessions", state.sessions[key].identity_id, state.sessions[key].slot
                ),
            ),
            "challenges": (
                _AttributePayloadView(state.challenges, "context"),
                lambda key: associated_data("ih_challenges", state.challenges[key].identity_id, str(key)),
            ),
            "verifications": (
                _AttributePayloadView(state.verifications, "evidence", skip_none=True),
                lambda key: associated_data("ih_verifications", key[0], key[1]),
            ),
            "proxies": (
                _AttributePayloadView(state.proxies, "secret"),
                lambda key: associated_data("ih_proxies", key),
            ),
            "webhooks": (
                _AttributePayloadView(state.webhook_endpoints, "secret"),
                lambda key: associated_data("ih_webhook_endpoints", key),
            ),
            "notification_channels": (
                _PayloadView(state.notification_channels, 1),
                lambda key: associated_data("ih_notification_channels", key),
            ),
            "profiles": (
                state.profile_secrets,
                lambda key: associated_data("ih_identities", key, "profile"),
            ),
            "import_items": (
                _ItemPayloadView(state.import_items, "payload"),
                lambda key: associated_data("ih_import_items", key[0], str(key[1])),
            ),
        }

    async def rotate_key(
        self, envelope: Any, *, table: str, limit: int, after: tuple[Any, ...] | None = None
    ) -> RotationBatch:
        active = envelope.active_key_id
        size = max(1, limit)
        async with self._lock:
            view, aad = self._rotation_views()[table]
            stale = sorted(
                (
                    (_rotation_cursor(key), key, payload)
                    for key, payload in list(view.items())
                    if isinstance(payload, EncryptedPayload) and payload.key_id != active
                ),
                key=lambda item: item[0],
            )
            page = [item for item in stale if after is None or item[0] > after][:size]
            rotated = 0
            unreadable: list[UnreadableSecret] = []
            for cursor, key, payload in page:
                try:
                    view[key] = envelope.rewrap(payload, aad(key), upgrade=table != "challenges")
                except DecryptionFailed:
                    if table == "credentials":
                        label = credential_label(*key)
                    elif table == "sessions":
                        record = self._state.sessions[key]
                        label = f"{record.identity_id}/{record.slot}"
                    else:
                        label = "/".join(cursor)
                    unreadable.append(UnreadableSecret(table, label))
                    continue
                rotated += 1
        last = page[-1][0] if len(page) >= size else None
        return RotationBatch(rotated, tuple(unreadable), last)

    async def rekey_secret_value_fingerprints(
        self,
        envelope: Any,
        fingerprints: Any,
        *,
        table: str,
        limit: int,
        after: tuple[Any, ...] | None = None,
    ) -> RotationBatch:
        if table not in {"credentials", "sessions"}:
            raise ValidationFailed("value fingerprints exist only for credentials and sessions")
        size = max(1, limit)
        async with self._lock:
            entries: list[tuple[tuple[str, ...], Any, StoredPayload, Any]]
            if table == "credentials":
                entries = [
                    (_rotation_cursor(key), key, payload, row)
                    for key, (payload, row) in self._state.credentials.items()
                ]
            else:
                entries = [
                    (_rotation_cursor(key), key, row.payload, row)
                    for key, row in self._state.sessions.items()
                ]
            page = [entry for entry in sorted(entries) if after is None or entry[0] > after][:size]
            updated = 0
            unreadable: list[UnreadableSecret] = []
            for _cursor, key, payload, row in page:
                aad = (
                    credential_associated_data(row.identity_id, row.kind, row.name)
                    if table == "credentials"
                    else associated_data("ih_sessions", row.identity_id, row.slot)
                )
                try:
                    value = (
                        payload.value
                        if isinstance(payload, PlaintextPayload)
                        else envelope.open_json(payload, aad)
                    )
                except DecryptionFailed:
                    label = (
                        credential_label(row.identity_id, row.kind, row.name)
                        if table == "credentials"
                        else f"{row.identity_id}/{row.slot}"
                    )
                    unreadable.append(UnreadableSecret(table, label))
                    continue
                canonical = json.dumps(
                    pydantic_core.to_jsonable_python(value),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                fingerprint = fingerprints.tag(canonical)
                if row.value_fingerprint == fingerprint:
                    continue
                if table == "credentials":
                    self._state.credentials[key] = (payload, replace(row, value_fingerprint=fingerprint))
                else:
                    self._state.sessions[key] = replace(row, value_fingerprint=fingerprint)
                updated += 1
        last = page[-1][0] if len(page) >= size else None
        return RotationBatch(updated, tuple(unreadable), last)

    async def inactive_key_usage(self, active: str) -> dict[str, dict[str, int]]:
        usage: dict[str, dict[str, int]] = {}
        async with self._lock:
            for name, (view, _aad) in self._rotation_views().items():
                counts: dict[str, int] = defaultdict(int)
                for _key, payload in list(view.items()):
                    if isinstance(payload, EncryptedPayload) and payload.key_id != active:
                        counts[payload.key_id] += 1
                if counts:
                    usage[name] = dict(counts)
        return usage

    async def convert_secret_storage(
        self,
        envelope: Any,
        *,
        table: str,
        to: str,
        limit: int,
        space: str | None = None,
    ) -> SecretConversionBatch:
        async with self._lock:
            slug_by_space = {ref.id: ref.slug for ref in self._state.spaces.values()}
            pending: list[tuple[Any, StoredPayload, str]] = []
            view, aad_of = self._rotation_views()[table]
            rows = sorted(view.items(), key=lambda item: _rotation_cursor(item[0]))
            for key, payload in rows:
                space_id = self._secret_space_id(table, key)
                slug = slug_by_space[space_id]
                if space is not None and slug != space:
                    continue
                if (to == "plaintext") != isinstance(payload, EncryptedPayload):
                    continue
                converted = self._converted_payload(envelope, payload, aad_of(key), to)
                pending.append((key, converted, slug))
                if len(pending) == limit:
                    break
            for key, payload, _slug in pending:
                if table == "challenges":
                    row = self._state.challenges[key]
                    aad = aad_of(key)
                    if isinstance(row.context, EncryptedPayload) and row.answer_ciphertext is not None:
                        answer = envelope.open_sibling(
                            row.context, aad, row.answer_nonce, row.answer_ciphertext, "answer"
                        ).decode()
                        row = replace(
                            row,
                            answer_nonce=None,
                            answer_ciphertext=None,
                            answer_payload=answer,
                        )
                    elif isinstance(row.context, PlaintextPayload) and row.answer_payload is not None:
                        assert isinstance(payload, EncryptedPayload)
                        nonce, ciphertext = envelope.seal_sibling(
                            payload, aad, row.answer_payload.encode(), "answer"
                        )
                        row = replace(
                            row,
                            answer_nonce=nonce,
                            answer_ciphertext=ciphertext,
                            answer_payload=None,
                        )
                    self._state.challenges[key] = row
                view[key] = payload
            counts: dict[str, int] = defaultdict(int)
            for _key, _payload, slug in pending:
                counts[slug] += 1
            return SecretConversionBatch(len(pending), dict(counts))

    @staticmethod
    def _converted_payload(envelope: Any, payload: StoredPayload, aad: bytes, to: str) -> StoredPayload:
        if to == "plaintext":
            assert isinstance(payload, EncryptedPayload)
            value = envelope.open_json(payload, aad)
            if not isinstance(value, dict):
                raise ValidationFailed("секрет должен быть JSON-объектом")
            return PlaintextPayload(value)
        assert isinstance(payload, PlaintextPayload)
        return cast(EncryptedPayload, envelope.seal(pydantic_core.to_json(payload.value), aad))

    async def plaintext_secret_counts(self, *, space: str | None = None) -> dict[str, int]:
        async with self._lock:
            space_ids = {ref.id for ref in self._state.spaces.values() if space is None or ref.slug == space}
            counts = {
                table: sum(
                    isinstance(payload, PlaintextPayload) and self._secret_space_id(table, key) in space_ids
                    for key, payload in view.items()
                )
                for table, (view, _aad) in self._rotation_views().items()
            }
        return {name: count for name, count in counts.items() if count}

    def _secret_space_id(self, table: str, key: Any) -> UUID:
        state = self._state
        if table == "credentials":
            return state.identities[state.credentials[key][1].identity_id].space_id
        if table == "sessions":
            return state.identities[state.sessions[key].identity_id].space_id
        if table == "challenges":
            return state.identities[state.challenges[key].identity_id].space_id
        if table == "verifications":
            return state.identities[key[0]].space_id
        if table == "proxies":
            return state.proxies[key].space_id
        if table == "webhooks":
            return state.webhook_endpoints[key].space_id
        if table == "notification_channels":
            return state.notification_channels[key][2]
        if table == "profiles":
            return state.identities[key].space_id
        if table == "import_items":
            return state.import_runs[key[0]].space_id
        raise ValidationFailed(f"unknown secret table: {table}")

    async def metrics_snapshot(self, slugs: frozenset[str] | None = None) -> MetricsSnapshot:
        async with self._lock:
            external_ids = [event.id for event, _, external in self._state.events if external]
            cursor_lag = {
                consumer: sum(event_id > cursor for event_id in external_ids)
                for consumer, cursor in self._state.cursors.items()
            }
            allowed_space_ids = {
                ref.id for ref in self._state.spaces.values() if slugs is None or ref.slug in slugs
            }
            webhook_rows = [
                row
                for row in self._state.webhook_deliveries.values()
                if (endpoint := self._state.webhook_endpoints.get(row.endpoint_id)) is not None
                and endpoint.space_id in allowed_space_ids
            ]
            notification_rows = [
                row
                for row in self._state.notifications.values()
                if self._state.notification_rule_spaces.get(row["rule_id"]) in allowed_space_ids
            ]
            return MetricsSnapshot(
                cursor_lag,
                {
                    "webhooks": sum(row.status in {"pending", "failed"} for row in webhook_rows),
                    "notifications": sum(row["status"] in {"pending", "failed"} for row in notification_rows),
                },
                {},
                {
                    "webhooks": sum(row.status == "failed" for row in webhook_rows),
                    "notifications": sum(row["status"] == "failed" for row in notification_rows),
                },
                {
                    "webhooks": min(
                        (row.next_attempt_at for row in webhook_rows if row.status in {"pending", "failed"}),
                        default=None,
                    ),
                    "notifications": min(
                        (
                            row["scheduled_at"]
                            for row in notification_rows
                            if row["status"] in {"pending", "failed"}
                        ),
                        default=None,
                    ),
                },
                {
                    "webhooks": max(
                        (row.delivered_at for row in webhook_rows if row.delivered_at is not None),
                        default=None,
                    ),
                    "notifications": max(
                        (row["sent_at"] for row in notification_rows if row.get("sent_at") is not None),
                        default=None,
                    ),
                },
                {
                    "webhooks": any(row.error is not None for row in webhook_rows),
                    "notifications": any(row.get("error") is not None for row in notification_rows),
                },
            )


def _rotation_cursor(key: Any) -> tuple[str, ...]:
    """Первичный ключ записи как кортеж строк: порядок обхода и идентификатор в отчёте ротации."""
    return tuple(str(part) for part in (key if isinstance(key, tuple) else (key,)))


class _ItemPayloadView:
    """Изменяемое представление payload внутри записей-словарей (элементы импорта)."""

    def __init__(self, source: dict[Any, dict[str, Any]], field_name: str) -> None:
        self.source = source
        self.field_name = field_name

    def items(self) -> list[tuple[Any, StoredPayload]]:
        return [(key, row[self.field_name]) for key, row in self.source.items()]

    def __setitem__(self, key: Any, value: StoredPayload) -> None:
        self.source[key][self.field_name] = value


class _PayloadView:
    """Минимальное изменяемое отображение payload внутри tuple-записей для rotate_key."""

    def __init__(self, source: dict[Any, tuple[Any, ...]], index: int) -> None:
        self.source = source
        self.index = index

    def items(self) -> list[tuple[Any, StoredPayload]]:
        return [(key, row[self.index]) for key, row in self.source.items()]

    def __setitem__(self, key: Any, value: StoredPayload) -> None:
        row = list(self.source[key])
        row[self.index] = value
        self.source[key] = tuple(row)


class _AttributePayloadView:
    """Изменяемое представление dataclass-поля с зашифрованным payload."""

    def __init__(self, source: dict[Any, Any], attribute: str, *, skip_none: bool = False) -> None:
        self.source = source
        self.attribute = attribute
        self.skip_none = skip_none

    def items(self) -> list[tuple[Any, StoredPayload]]:
        rows = [(key, getattr(row, self.attribute)) for key, row in self.source.items()]
        return [(key, value) for key, value in rows if value is not None]

    def __setitem__(self, key: Any, value: StoredPayload) -> None:
        self.source[key] = replace(self.source[key], **{self.attribute: value})
