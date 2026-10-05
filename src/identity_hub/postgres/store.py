"""PostgreSQL-реализация хранилища. Весь SQL библиотеки живёт здесь (D-008).

Правила (ТЗ §7, §8):
- своя транзакция — `sessionmaker()` + `begin()`; сессия пользователя — только `begin_nested()` (T-1);
- `set_config('ih.space', slug, true)` в начале каждой транзакции (T-3);
- таблицы — только Core-конструкции с `schema_translate_map` (D-013), без text() с именами таблиц.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    and_,
    any_,
    case,
    delete,
    func,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.engine import Result
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Executable, Select

from identity_hub._ids import new_id
from identity_hub.crypto.envelope import (
    DecryptionFailed,
    EncryptedPayload,
    associated_data,
    credential_associated_data,
)
from identity_hub.domain.idempotency import FINGERPRINT_VERSION, LEGACY_FINGERPRINT_VERSION
from identity_hub.domain.region_specs import AVAILABILITY, STANDING
from identity_hub.domain.regions import RegionState
from identity_hub.enums import (
    AuthState,
    Availability,
    ChangeSource,
    DeliveryStatus,
    LeaseEndReason,
    Lifecycle,
    ProxyStatus,
    Severity,
    VerificationState,
)
from identity_hub.errors import (
    ConcurrentModification,
    DatabaseBusy,
    IdempotencyConflict,
    IdentityHubError,
    LeaseOwnershipLost,
    LeaseUnavailable,
    NotFound,
    RevisionConflict,
    SpaceMismatch,
    StorageError,
    describe_error,
)
from identity_hub.events import Event, EventDraft
from identity_hub.postgres import schema_options
from identity_hub.postgres.migrate import SCHEMA_COMPATIBLE, SCHEMA_HEAD, VERSION_TABLE
from identity_hub.postgres.models import (
    XID8,
    BindingRecord,
    ChallengeRecord,
    CredentialsRecord,
    EventCursorRecord,
    EventRecord,
    IdentityDataRecord,
    IdentityLinkRecord,
    IdentityRecord,
    ImportItemRecord,
    ImportRunRecord,
    LeaseHistoryRecord,
    LeaseLogRecord,
    LeaseRecord,
    NotificationChannelRecord,
    NotificationRecord,
    NotificationRuleRecord,
    OperationKeyRecord,
    ProviderOperationRecord,
    ProxyRecord,
    QuotaUsageRecord,
    RegionStateRecord,
    ServiceRecord,
    SessionRecord,
    SpaceRecord,
    StateSnapshotCoverageRecord,
    StateSnapshotRecord,
    StatsMetricBatchRecord,
    StatsRollupRecord,
    VerificationRecord,
    WebhookDeliveryRecord,
    WebhookEndpointRecord,
)
from identity_hub.rollups import BucketSize, RollupDelta, RollupKey, RollupRow
from identity_hub.snapshots import StateSnapshotFrame, StateSnapshotRow
from identity_hub.store.base import (
    NOTIFY_CHANNEL,
    UNREADABLE_IMPORT_ITEM,
    AttentionChallengeRow,
    AttentionIdentityRow,
    CandidateQuery,
    ChallengeRow,
    ChargeOutcome,
    CredentialMetadataRow,
    CredentialRow,
    DataRow,
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
    LeaseReadRow,
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
    ServiceRow,
    SessionMetadataRow,
    SpaceRef,
    Store,
    StoreTx,
    UnreadableSecret,
    VerificationRow,
    WebhookAttempt,
    WebhookDeliveryRow,
    WebhookEndpointReadRow,
    WebhookEndpointRow,
    credential_label,
)

__all__ = ["NOTIFY_CHANNEL", "PgStore", "PgTx"]

IDENTITIES = cast(Table, IdentityRecord.__table__)
IMPORT_RUNS = cast(Table, ImportRunRecord.__table__)
IMPORT_ITEMS = cast(Table, ImportItemRecord.__table__)
REGION_STATES = cast(Table, RegionStateRecord.__table__)
CREDENTIALS = cast(Table, CredentialsRecord.__table__)
SESSIONS = cast(Table, SessionRecord.__table__)
LINKS = cast(Table, IdentityLinkRecord.__table__)
DATA = cast(Table, IdentityDataRecord.__table__)
LEASES = cast(Table, LeaseRecord.__table__)
HISTORY = cast(Table, LeaseHistoryRecord.__table__)
LEASE_LOGS = cast(Table, LeaseLogRecord.__table__)
QUOTAS = cast(Table, QuotaUsageRecord.__table__)
OPERATION_KEYS = cast(Table, OperationKeyRecord.__table__)
PROVIDER_OPERATIONS = cast(Table, ProviderOperationRecord.__table__)
PROXIES = cast(Table, ProxyRecord.__table__)
BINDINGS = cast(Table, BindingRecord.__table__)
CHALLENGES = cast(Table, ChallengeRecord.__table__)
VERIFICATIONS = cast(Table, VerificationRecord.__table__)
EVENTS = cast(Table, EventRecord.__table__)
CURSORS = cast(Table, EventCursorRecord.__table__)
ROLLUPS = cast(Table, StatsRollupRecord.__table__)
METRIC_BATCHES = cast(Table, StatsMetricBatchRecord.__table__)
STATE_SNAPSHOTS = cast(Table, StateSnapshotRecord.__table__)
SNAPSHOT_COVERAGE = cast(Table, StateSnapshotCoverageRecord.__table__)
SPACES = cast(Table, SpaceRecord.__table__)
WEBHOOK_ENDPOINTS = cast(Table, WebhookEndpointRecord.__table__)
WEBHOOK_DELIVERIES = cast(Table, WebhookDeliveryRecord.__table__)
LISTEN_PROBE_TIMEOUT = 5.0  # секунд ждать пробное уведомление при подписке LISTEN
_logger = logging.getLogger("identity_hub")
DOCTOR_PAGE = 1000  # аккаунтов на одну транзакцию doctor
_OPEN_DELIVERY = (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
# результат попытки принимается и у доставки, поставленной на паузу после захвата
_FINISHABLE_DELIVERY = (*_OPEN_DELIVERY, DeliveryStatus.PAUSED)


def _any_of(column: Any, values: Iterable[Any]) -> Any:
    """`column = ANY(:values)` одним параметром-массивом.

    `IN (…)` с длинным списком упирается в предел 32 767 параметров запроса asyncpg (R2-D06).
    """
    return column == any_(literal(list(values), ARRAY(column.type)))


def _pause_deliveries(endpoint_id: UUID) -> Any:
    """Очередь отключённого endpoint'а уходит из частичного индекса claim (R2-D15)."""
    d = WEBHOOK_DELIVERIES.c
    return (
        update(WEBHOOK_DELIVERIES)
        .where(d.endpoint_id == endpoint_id, d.status.in_(_OPEN_DELIVERY))
        .values(status=DeliveryStatus.PAUSED)
    )


def _resume_deliveries(endpoint_id: UUID) -> Any:
    d = WEBHOOK_DELIVERIES.c
    return (
        update(WEBHOOK_DELIVERIES)
        .where(d.endpoint_id == endpoint_id, d.status == DeliveryStatus.PAUSED)
        .values(status=DeliveryStatus.PENDING, next_attempt_at=func.now())
    )


NOTIFICATION_CHANNELS = cast(Table, NotificationChannelRecord.__table__)
NOTIFICATION_RULES = cast(Table, NotificationRuleRecord.__table__)
NOTIFICATIONS = cast(Table, NotificationRecord.__table__)
PROXY_FIELDS = tuple(f.name for f in fields(ProxyRow))
LEASE_HISTORY_FIELDS = tuple(f.name for f in fields(LeaseHistoryRow))
IDENTITY_FIELDS = tuple(f.name for f in fields(IdentityRow))
IMPORT_RUN_FIELDS = tuple(f.name for f in fields(ImportRunRow))
PROVIDER_OPERATION_FIELDS = tuple(f.name for f in fields(ProviderOperationRow))
REGION_FIELDS = tuple(f.name for f in fields(RegionState))

SESSION_SPACE_KEY = "identity_hub.space"


def _requirement_deadline(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _optional_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        return None


class PgTx(StoreTx):
    def __init__(self, store: PgStore, session: AsyncSession, space: SpaceRef) -> None:
        self.store = store
        self.session = session
        self.space = space
        self._now: datetime | None = None
        self._revised: set[UUID] = set()  # identity, чья ревизия уже поднята этой командой (D-205)

    async def execute(self, stmt: Executable, params: Any = None) -> Result[Any]:
        return await _execute(self.session, stmt, params, execution_options=self.store.options)

    async def now(self) -> datetime:
        if self._now is None:
            self._now = (await _execute(self.session, select(func.now()))).scalar_one()
        return self._now

    async def emit(self, drafts: Sequence[EventDraft]) -> list[UUID]:
        if not drafts:
            return []
        rows: list[dict[str, Any]] = []
        for draft in drafts:
            sequence: int | None = None
            revision: int | None = None
            if draft.identity_id is not None:
                bump = draft.spec.aggregate and draft.identity_id not in self._revised
                counters: Any = (
                    await self.execute(
                        update(IdentityRecord)
                        .where(
                            IdentityRecord.id == draft.identity_id, IdentityRecord.space_id == self.space.id
                        )
                        .values(
                            event_seq=IdentityRecord.event_seq + 1,
                            revision=IdentityRecord.revision + int(bump),
                        )
                        .returning(IdentityRecord.event_seq, IdentityRecord.revision)
                    )
                ).one_or_none()
                if counters is not None:
                    sequence, revision = int(counters[0]), int(counters[1])
                    if bump:
                        self._revised.add(draft.identity_id)
            rows.append(
                {
                    "event_id": new_id(),
                    "type": draft.type,
                    "severity": str(draft.effective_severity),
                    "space_id": self.space.id,
                    "service": draft.service,
                    "identity_id": draft.identity_id,
                    "proxy_id": draft.proxy_id,
                    "lease_id": draft.lease_id,
                    "sequence": sequence,
                    "revision": revision,
                    "actor": draft.actor,
                    "data": dict(draft.data),
                    "external": draft.spec.external,
                }
            )
        await self.execute(insert(EventRecord).values(rows))
        if any(r["external"] for r in rows):
            # доставляется слушателям при коммите; при откате — нет (FR-13.6)
            await _execute(
                self.session,
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": NOTIFY_CHANNEL, "payload": self.space.slug},
            )
        return [r["event_id"] for r in rows]

    # --------------------------------------------------------- аккаунт и регионы

    async def get_identity(self, identity_id: UUID) -> IdentityRow | None:
        row = (
            (
                await self.execute(
                    select(IDENTITIES).where(
                        IDENTITIES.c.id == identity_id, IDENTITIES.c.space_id == self.space.id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return IdentityRow(**{f: row[f] for f in IDENTITY_FIELDS}) if row else None

    async def lock_identity_revision(self, identity_id: UUID, expected_revision: int) -> int:
        revision = (
            await self.execute(
                select(IDENTITIES.c.revision)
                .where(IDENTITIES.c.id == identity_id, IDENTITIES.c.space_id == self.space.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if revision is None:
            raise NotFound("identity", identity_id)
        actual = int(revision)
        if actual != expected_revision:
            raise RevisionConflict("identity", identity_id, expected_revision, actual)
        return actual

    async def update_identity(
        self, identity_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        stmt = (
            update(IDENTITIES)
            .where(IDENTITIES.c.id == identity_id, IDENTITIES.c.space_id == self.space.id)
            .values(**values, updated_at=func.now())
        )
        for column, value in (expected or {}).items():
            stmt = stmt.where(IDENTITIES.c[column].is_not_distinct_from(value))
        result = await self.execute(stmt)
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def load_region(self, identity_id: UUID, region: str, key: str) -> RegionState | None:
        row = (
            (
                await self.execute(
                    select(REGION_STATES).where(
                        REGION_STATES.c.identity_id == identity_id,
                        REGION_STATES.c.space_id == self.space.id,
                        REGION_STATES.c.region == region,
                        REGION_STATES.c.key == key,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        values = {f: row[f] for f in REGION_FIELDS}
        values["change_source"] = ChangeSource(values["change_source"])
        return RegionState(**values)

    async def save_region(
        self, identity_id: UUID, region: str, key: str, previous: RegionState | None, new: RegionState
    ) -> bool:
        values = {f: getattr(new, f) for f in REGION_FIELDS}
        values["change_source"] = str(new.change_source)
        if previous is None:
            stmt = (
                insert(REGION_STATES)
                .values(identity_id=identity_id, space_id=self.space.id, region=region, key=key, **values)
                .on_conflict_do_nothing()
            )
            return bool((await self.execute(stmt)).rowcount)  # type: ignore[attr-defined]
        stmt_upd = (
            update(REGION_STATES)
            .where(
                REGION_STATES.c.identity_id == identity_id,
                REGION_STATES.c.space_id == self.space.id,
                REGION_STATES.c.region == region,
                REGION_STATES.c.key == key,
                REGION_STATES.c.version == previous.version,
            )
            .values(**values)
        )
        return bool((await self.execute(stmt_upd)).rowcount)  # type: ignore[attr-defined]

    # --------------------------------------------------------- регистрация

    async def find_identity(
        self, identity_id: UUID, service: str, normalized_ref: str
    ) -> tuple[UUID, UUID] | None:
        row = (
            await self.execute(
                select(IDENTITIES.c.id, IDENTITIES.c.space_id)
                .where(
                    or_(
                        IDENTITIES.c.id == identity_id,
                        and_(IDENTITIES.c.service == service, IDENTITIES.c.normalized_ref == normalized_ref),
                    )
                )
                .limit(1)
            )
        ).one_or_none()
        return (row.id, row.space_id) if row else None

    async def insert_identity(self, row: NewIdentity) -> bool:
        values: dict[str, Any] = {
            "id": row.id,
            "space_id": self.space.id,
            "service": row.service,
            "external_ref": row.external_ref,
            "normalized_ref": row.unique_ref,
            "normalize_version": row.normalize_version,
            "lifecycle": row.lifecycle,
            "standing": row.standing,
            "auth_state": row.auth_state,
            "tags": row.tags,
            "attributes": row.attributes,
            "profile": row.profile,
            "labels": row.labels,
            "note": row.note,
            "search_text": row.search_text,
        }
        if row.profile_secret is not None:
            values |= row.profile_secret.columns("profile_")
        stmt = insert(IDENTITIES).values(**values).on_conflict_do_nothing()
        return bool((await self.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def find_by_refs(self, refs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], tuple[UUID, UUID]]:
        if not refs:
            return {}
        pairs = list(dict.fromkeys(refs))
        rows = await self.execute(
            select(
                IDENTITIES.c.id, IDENTITIES.c.space_id, IDENTITIES.c.service, IDENTITIES.c.normalized_ref
            ).where(tuple_(IDENTITIES.c.service, IDENTITIES.c.normalized_ref).in_(pairs))
        )
        return {(r.service, r.normalized_ref): (r.id, r.space_id) for r in rows}

    async def existing_identities(self, ids: Sequence[UUID]) -> set[UUID]:
        if not ids:
            return set()
        rows = await self.execute(
            select(IDENTITIES.c.id).where(
                _any_of(IDENTITIES.c.id, ids), IDENTITIES.c.space_id == self.space.id
            )
        )
        return set(rows.scalars())

    async def put_credentials(self, identity_id: UUID, payload: EncryptedPayload) -> None:
        await self.put_credential(identity_id, "default", "default", payload)

    async def put_credential(
        self,
        identity_id: UUID,
        kind: str,
        name: str,
        payload: EncryptedPayload,
        *,
        public_value: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> CredentialRow:
        c = CREDENTIALS.c
        stmt = insert(CREDENTIALS).values(
            id=new_id(),
            identity_id=identity_id,
            space_id=self.space.id,
            kind=kind,
            name=name,
            public_value=public_value,
            expires_at=expires_at,
            **payload.columns(),
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[c.identity_id, c.kind, c.name],
            set_={
                **payload.columns(),
                "public_value": stmt.excluded.public_value,
                "expires_at": stmt.excluded.expires_at,
                "version": c.version + 1,
                "revision": c.revision + 1,
                "updated_at": func.now(),
            },
        ).returning(*_CREDENTIAL_COLUMNS)
        return _credential_row((await self.execute(upsert)).mappings().one())

    async def get_credential(
        self, identity_id: UUID, kind: str, name: str
    ) -> tuple[EncryptedPayload, CredentialRow] | None:
        c = CREDENTIALS.c
        row = (
            (
                await self.execute(
                    select(*_CREDENTIAL_COLUMNS, c.key_id, c.wrapped_dek, c.nonce, c.ciphertext).where(
                        c.identity_id == identity_id,
                        c.space_id == self.space.id,
                        c.kind == kind,
                        c.name == name,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        payload = EncryptedPayload(row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"])
        return payload, _credential_row(row)

    async def list_credentials(self, identity_id: UUID) -> list[CredentialRow]:
        c = CREDENTIALS.c
        rows = await self.execute(
            select(*_CREDENTIAL_COLUMNS)
            .where(c.identity_id == identity_id, c.space_id == self.space.id)
            .order_by(c.kind, c.name)
        )
        return [_credential_row(row) for row in rows.mappings()]

    async def put_session(
        self,
        identity_id: UUID,
        slot: str,
        payload: EncryptedPayload,
        *,
        fencing: int,
        expires_at: datetime | None = None,
    ) -> bool:
        stmt = insert(SESSIONS).values(
            identity_id=identity_id,
            space_id=self.space.id,
            slot=slot,
            status="valid",
            expires_at=expires_at,
            written_by_fencing=fencing,
            **payload.columns(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SESSIONS.c.identity_id, SESSIONS.c.slot],
            set_={
                **payload.columns(),
                "status": "valid",
                "expires_at": expires_at,
                "written_by_fencing": fencing,
                "version": SESSIONS.c.version + 1,
                "updated_at": func.now(),
            },
            where=SESSIONS.c.written_by_fencing <= fencing,
        )
        return bool((await self.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def insert_link(self, identity_id: UUID, role: str, linked_identity_id: UUID) -> bool:
        stmt = (
            insert(LINKS)
            .values(
                identity_id=identity_id,
                role=role,
                space_id=self.space.id,
                linked_identity_id=linked_identity_id,
            )
            .on_conflict_do_nothing()
        )
        return bool((await self.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    # --------------------------------------------------------- данные аккаунта

    async def get_profile_secret(self, identity_id: UUID) -> EncryptedPayload | None:
        c = IDENTITIES.c
        row = (
            await self.execute(
                select(c.profile_key_id, c.profile_wrapped_dek, c.profile_nonce, c.profile_ciphertext).where(
                    c.id == identity_id, c.space_id == self.space.id
                )
            )
        ).one_or_none()
        if row is None or row.profile_ciphertext is None:
            return None
        return EncryptedPayload(
            row.profile_key_id, row.profile_wrapped_dek, row.profile_nonce, row.profile_ciphertext
        )

    async def set_profile_secret(self, identity_id: UUID, payload: EncryptedPayload | None) -> None:
        values: dict[str, Any] = (
            payload.columns("profile_")
            if payload
            else dict.fromkeys(
                ("profile_key_id", "profile_wrapped_dek", "profile_nonce", "profile_ciphertext")
            )
        )
        await self.update_identity(identity_id, values)

    async def incoming_links(self, identity_id: UUID) -> list[tuple[UUID, str]]:
        rows = await self.execute(
            select(LINKS.c.identity_id, LINKS.c.role).where(
                LINKS.c.linked_identity_id == identity_id, LINKS.c.space_id == self.space.id
            )
        )
        return [(r.identity_id, r.role) for r in rows]

    async def delete_identity(self, identity_id: UUID) -> bool:
        bound = (
            await self.execute(select(BINDINGS.c.proxy_id).where(BINDINGS.c.identity_id == identity_id))
        ).scalar_one_or_none()
        if bound is not None:  # ёмкость прокси освобождается вместе с привязкой (D-117)
            await self.execute(
                update(PROXIES)
                .where(PROXIES.c.id == bound, PROXIES.c.bound_count > 0)
                .values(bound_count=PROXIES.c.bound_count - 1)
            )
        # Строка ih_leases уйдёт каскадом: истёкшую, но ещё не закрытую чистильщиком аренду
        # закрываем здесь, иначе её история осталась бы открытой навсегда (R2-D14).
        await self.execute(
            update(HISTORY)
            .where(
                HISTORY.c.lease_id == LEASES.c.lease_id,
                LEASES.c.identity_id == identity_id,
                LEASES.c.space_id == self.space.id,
                HISTORY.c.released_at.is_(None),
            )
            .values(released_at=LEASES.c.expires_at, end_reason=LeaseEndReason.EXPIRED)
        )
        result = await self.execute(
            delete(IDENTITIES).where(IDENTITIES.c.id == identity_id, IDENTITIES.c.space_id == self.space.id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def load_data(self, identity_id: UUID, namespace: str) -> DataRow | None:
        row = (
            await self.execute(
                select(DATA.c.data, DATA.c.schema_version, DATA.c.version, DATA.c.updated_at).where(
                    DATA.c.identity_id == identity_id,
                    DATA.c.namespace == namespace,
                    DATA.c.space_id == self.space.id,
                )
            )
        ).one_or_none()
        return DataRow(row.data, row.schema_version, row.version, row.updated_at) if row else None

    async def save_data(
        self,
        identity_id: UUID,
        namespace: str,
        data: dict[str, Any],
        schema_version: int,
        expected_version: int | None,
    ) -> int | None:
        stmt: Executable
        if expected_version is None:
            stmt = (
                insert(DATA)
                .values(
                    identity_id=identity_id,
                    namespace=namespace,
                    space_id=self.space.id,
                    data=data,
                    schema_version=schema_version,
                    version=1,
                )
                .on_conflict_do_nothing()
                .returning(DATA.c.version)
            )
        else:
            stmt = (
                update(DATA)
                .where(
                    DATA.c.identity_id == identity_id,
                    DATA.c.namespace == namespace,
                    DATA.c.space_id == self.space.id,
                    DATA.c.version == expected_version,
                )
                .values(
                    data=data,
                    schema_version=schema_version,
                    version=DATA.c.version + 1,
                    updated_at=func.now(),
                )
                .returning(DATA.c.version)
            )
        version: int | None = (await self.execute(stmt)).scalar_one_or_none()
        return version

    # --------------------------------------------------------- связи

    async def delete_link(self, identity_id: UUID, role: str) -> UUID | None:
        result = await self.execute(
            delete(LINKS)
            .where(
                LINKS.c.identity_id == identity_id, LINKS.c.role == role, LINKS.c.space_id == self.space.id
            )
            .returning(LINKS.c.linked_identity_id)
        )
        linked: UUID | None = result.scalar_one_or_none()
        return linked

    async def links_of(self, identity_id: UUID) -> tuple[list[LinkRow], list[LinkRow]]:
        other = IDENTITIES.alias("other")
        out_rows = await self.execute(
            select(LINKS, other.c.service, other.c.external_ref)
            .join(other, other.c.id == LINKS.c.linked_identity_id)
            .where(LINKS.c.identity_id == identity_id, LINKS.c.space_id == self.space.id)
            .order_by(LINKS.c.role)
        )
        in_rows = await self.execute(
            select(LINKS, other.c.service, other.c.external_ref)
            .join(other, other.c.id == LINKS.c.identity_id)
            .where(LINKS.c.linked_identity_id == identity_id, LINKS.c.space_id == self.space.id)
            .order_by(LINKS.c.role, LINKS.c.identity_id)
        )

        def build(rows: Any) -> list[LinkRow]:
            return [
                LinkRow(r.identity_id, r.role, r.linked_identity_id, r.service, r.external_ref, r.created_at)
                for r in rows
            ]

        return build(out_rows), build(in_rows)

    async def link_counts(self, ids: Sequence[UUID]) -> dict[UUID, tuple[int, int, list[str]]]:
        if not ids:
            return {}
        id_list = list(ids)
        outgoing = (
            select(
                LINKS.c.identity_id.label("id"),
                func.count().label("n"),
                func.array_agg(LINKS.c.role).label("roles"),
            )
            .where(_any_of(LINKS.c.identity_id, id_list), LINKS.c.space_id == self.space.id)
            .group_by(LINKS.c.identity_id)
            .subquery()
        )
        incoming = (
            select(LINKS.c.linked_identity_id.label("id"), func.count().label("n"))
            .where(_any_of(LINKS.c.linked_identity_id, id_list), LINKS.c.space_id == self.space.id)
            .group_by(LINKS.c.linked_identity_id)
            .subquery()
        )
        ids_values = select(func.unnest(cast_uuid_array(id_list)).label("id")).subquery()
        rows = await self.execute(
            select(ids_values.c.id, outgoing.c.n, outgoing.c.roles, incoming.c.n.label("incoming"))
            .outerjoin(outgoing, outgoing.c.id == ids_values.c.id)
            .outerjoin(incoming, incoming.c.id == ids_values.c.id)
        )
        return {r.id: (r.n or 0, r.incoming or 0, sorted(r.roles or [])) for r in rows}

    async def dependents(self, identity_id: UUID, max_depth: int) -> list[tuple[UUID, int, list[str]]]:
        base = (
            select(
                LINKS.c.identity_id.label("id"),
                literal(1).label("depth"),
                pg_array([sa_cast(LINKS.c.role, Text)]).label("path"),
                pg_array([LINKS.c.identity_id]).label("seen"),
            )
            .where(LINKS.c.linked_identity_id == identity_id, LINKS.c.space_id == self.space.id)
            .cte("impact", recursive=True)
        )
        step = (
            select(
                LINKS.c.identity_id,
                base.c.depth + 1,
                base.c.path.op("||")(pg_array([sa_cast(LINKS.c.role, Text)])),
                base.c.seen.op("||")(pg_array([LINKS.c.identity_id])),
            )
            .join(base, LINKS.c.linked_identity_id == base.c.id)
            .where(
                base.c.depth < max_depth,
                LINKS.c.space_id == self.space.id,
                ~(LINKS.c.identity_id == func.any(base.c.seen)),
                LINKS.c.identity_id != identity_id,
            )
        )
        impact = base.union_all(step)
        rows = await self.execute(select(impact.c.id, impact.c.depth, impact.c.path).order_by(impact.c.depth))
        best: dict[UUID, tuple[UUID, int, list[str]]] = {}
        for r in rows:
            best.setdefault(r.id, (r.id, r.depth, list(r.path)))
        result = list(best.values())
        return sorted(result, key=lambda item: (item[1], str(item[0])))

    # --------------------------------------------------------- аренда

    def _candidate_conditions(self, query: CandidateQuery) -> list[Any]:
        i, lease = IDENTITIES.c, LEASES.c
        now = func.now()
        conditions: list[Any] = [
            i.space_id == self.space.id,
            i.service == query.service,
            i.lifecycle.in_(sorted(query.lifecycles)),
            i.standing.in_(sorted(query.standing)),
            or_(
                i.availability == Availability.AVAILABLE,
                and_(
                    i.availability.in_([Availability.PAUSED, Availability.COOLING]),
                    i.availability_until <= now,
                ),
            ),
        ]
        if query.auth_states is not None:
            conditions.append(i.auth_state.in_(sorted(query.auth_states)))
        elif query.needs_auth:
            conditions.append(i.auth_state == AuthState.AUTHENTICATED)
        if query.required_factors:
            conditions.append(i.verified_factors.contains(sorted(query.required_factors)))
            # фактор с вышедшим сроком действия допуска не даёт и до прогона обслуживания (R2-F13)
            expired = (
                select(REGION_STATES.c.identity_id)
                .where(
                    REGION_STATES.c.identity_id == i.id,
                    REGION_STATES.c.region == "verification",
                    REGION_STATES.c.key.in_(sorted(query.required_factors)),
                    REGION_STATES.c.state == VerificationState.VERIFIED,
                    REGION_STATES.c.until <= now,
                )
                .exists()
            )
            conditions.append(~expired)
        if query.tags:
            conditions.append(i.tags.contains(sorted(query.tags)))
        if query.labels:
            conditions.append(i.labels.contains(query.labels))
        if query.profile:
            conditions.append(i.profile.contains(query.profile))
        if query.among is not None:
            among = query.among if isinstance(query.among, Select) else list(query.among)
            conditions.append(i.id.in_(among))
        if query.exclude:
            conditions.append(i.id.not_in(list(query.exclude)))
        if not query.shared:
            # NOT EXISTS, а не outer join с OR: иначе планировщик недооценивает число строк и
            # соединяет хешем все живые аренды с сортировкой вместо обхода индекса LRU (D-209)
            live = select(lease.identity_id).where(lease.identity_id == i.id, lease.expires_at > now)
            conditions.append(~live.exists())
        if query.proxy_wait:
            down = (
                select(BINDINGS.c.identity_id)
                .join(PROXIES, PROXIES.c.id == BINDINGS.c.proxy_id)
                .where(BINDINGS.c.identity_id == i.id, PROXIES.c.status != ProxyStatus.ALIVE)
            )
            conditions.append(~down.exists())
        return conditions

    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
        i = IDENTITIES.c
        conditions = self._candidate_conditions(query)
        # IMP-01 (D-209): наименее недавно арендованный по `last_leased_at` — порядок индекса
        # ix_ih_identities_pick. Захват ставит время, и занятые уходят в хвост: выборка не перебирает их.
        stmt = (
            select(IDENTITIES)
            .where(*conditions)
            .order_by(i.last_leased_at.asc().nulls_first(), i.id)
            .limit(1)
            .with_for_update(of=IDENTITIES, skip_locked=True)
        )
        row = (await self.execute(stmt)).mappings().one_or_none()
        return IdentityRow(**{f: row[f] for f in IDENTITY_FIELDS}) if row else None

    async def count_candidates(self, query: CandidateQuery, limit: int) -> int:
        i = IDENTITIES.c
        capped = select(i.id).where(*self._candidate_conditions(query)).limit(limit).subquery()
        return int((await self.execute(select(func.count()).select_from(capped))).scalar_one())

    async def count_live_candidates(self, query: CandidateQuery, limit: int) -> int:
        i, lease = IDENTITIES.c, LEASES.c
        unrestricted = replace(query, shared=True)
        capped = (
            select(i.id)
            .select_from(IDENTITIES.join(LEASES, lease.identity_id == i.id))
            .where(*self._candidate_conditions(unrestricted), lease.expires_at > func.now())
            .limit(limit)
            .subquery()
        )
        return int((await self.execute(select(func.count()).select_from(capped))).scalar_one())

    async def find_live_lease(self, idempotency_key: str) -> LeaseRow | None:
        row = (
            (
                await self.execute(
                    select(LEASES).where(
                        LEASES.c.idempotency_key == idempotency_key,
                        LEASES.c.space_id == self.space.id,
                        LEASES.c.expires_at > func.now(),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return _lease_row(row) if row else None

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
        now = await self.now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        # shared-аренды не вытесняют друг друга: fencing растёт только при эксклюзивном захвате (D-140);
        # время захвата ставят обе — по нему идёт LRU-выборка (IMP-01, D-209)
        values: dict[str, Any] = {"last_leased_at": func.clock_timestamp()}
        if not shared:
            values["fencing_seq"] = IDENTITIES.c.fencing_seq + 1
        fencing_stmt = (
            update(IDENTITIES)
            .values(**values)
            .returning(IDENTITIES.c.fencing_seq)
            .where(IDENTITIES.c.id == identity.id, IDENTITIES.c.space_id == self.space.id)
        )
        fencing: int = (await self.execute(fencing_stmt)).scalar_one()
        lease_id, token = new_id(), uuid4()
        reclaimed: LeaseRow | None = None
        if not shared:
            old = (
                (
                    await self.execute(
                        select(LEASES).where(LEASES.c.identity_id == identity.id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if old is not None:
                if old["expires_at"] > now:
                    raise LeaseUnavailable(f"аккаунт {identity.id} уже в аренде")
                closed = await self.execute(
                    update(HISTORY)
                    .where(HISTORY.c.lease_id == old["lease_id"], HISTORY.c.released_at.is_(None))
                    .values(released_at=old["expires_at"], end_reason=LeaseEndReason.EXPIRED)
                )
                if closed.rowcount:  # type: ignore[attr-defined]
                    reclaimed = _lease_row(old)
            values = {
                "space_id": self.space.id,
                "lease_id": lease_id,
                "token": token,
                "fencing": fencing,
                "pool": pool,
                "holder": holder,
                "idempotency_key": idempotency_key,
                "acquired_at": now,
                "heartbeat_at": now,
                "expires_at": expires_at,
            }
            stmt = insert(LEASES).values(identity_id=identity.id, **values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[LEASES.c.identity_id],
                set_=values,
                where=LEASES.c.expires_at <= func.now(),
            )
            if not (await self.execute(stmt)).rowcount:  # type: ignore[attr-defined]
                raise LeaseUnavailable(f"аккаунт {identity.id} уже в аренде")  # pragma: no cover
        await self.execute(
            insert(HISTORY).values(
                lease_id=lease_id,
                space_id=self.space.id,
                identity_id=identity.id,
                token=token,
                fencing=fencing,
                pool=pool,
                holder=holder,
                proxy_id=proxy_id,
                acquired_at=now,
                last_heartbeat_at=now,
            )
        )
        lease = LeaseRow(
            lease_id=lease_id,
            token=token,
            identity_id=identity.id,
            fencing=fencing,
            pool=pool,
            holder=holder,
            acquired_at=now,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            proxy_id=proxy_id,
            heartbeat_at=now,
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
    ) -> datetime | None:
        expires_at: datetime | None
        if shared:
            expires_at = await self.now() + timedelta(seconds=ttl_seconds)
        else:
            expires_at = (
                await self.execute(
                    update(LEASES)
                    .where(
                        LEASES.c.identity_id == lease.identity_id,
                        LEASES.c.token == lease.token,
                        LEASES.c.expires_at > func.now(),
                    )
                    .values(
                        heartbeat_at=func.now(),
                        expires_at=func.greatest(
                            LEASES.c.expires_at, func.now() + timedelta(seconds=ttl_seconds)
                        ),
                    )
                    .returning(LEASES.c.expires_at)
                )
            ).scalar_one_or_none()
            if expires_at is None:
                return None
            if not flush:
                return cast(datetime, expires_at)
        updated = await self.execute(
            update(HISTORY)
            .where(
                HISTORY.c.lease_id == lease.lease_id,
                HISTORY.c.token == lease.token,
                HISTORY.c.released_at.is_(None),
            )
            .values(last_heartbeat_at=func.now(), **_counter_values(counters))
        )
        if shared and not updated.rowcount:  # type: ignore[attr-defined]
            return None
        return expires_at

    async def release_lease(
        self, lease: LeaseRow, reason: str, counters: LeaseCounters, *, shared: bool
    ) -> str:
        history = (
            await self.execute(
                select(HISTORY.c.released_at, HISTORY.c.end_reason)
                .where(HISTORY.c.lease_id == lease.lease_id, HISTORY.c.token == lease.token)
                .with_for_update()
            )
        ).one_or_none()
        if history is None or history.released_at is not None:
            revoked = history is not None and history.end_reason == LeaseEndReason.REVOKED
            return "revoked" if revoked else "closed"
        released_at: Any = func.now()
        end_reason = reason
        if not shared:
            live = (
                await self.execute(
                    update(LEASES)
                    .where(
                        LEASES.c.identity_id == lease.identity_id,
                        LEASES.c.token == lease.token,
                        LEASES.c.expires_at > func.now(),
                    )
                    .values(expires_at=func.now(), idempotency_key=None)
                    .returning(LEASES.c.lease_id)
                )
            ).scalar_one_or_none()
            if live is None:
                # срок вышел, аккаунт никто не взял: история закрывается как истёкшая (D-213)
                expired_at = (
                    await self.execute(
                        update(LEASES)
                        .where(LEASES.c.identity_id == lease.identity_id, LEASES.c.token == lease.token)
                        .values(idempotency_key=None)
                        .returning(LEASES.c.expires_at)
                    )
                ).scalar_one_or_none()
                released_at = expired_at if expired_at is not None else func.now()
                end_reason = LeaseEndReason.EXPIRED
        await self.execute(
            update(HISTORY)
            .where(HISTORY.c.lease_id == lease.lease_id, HISTORY.c.released_at.is_(None))
            .values(released_at=released_at, end_reason=end_reason, **_counter_values(counters))
        )
        return "expired" if end_reason == LeaseEndReason.EXPIRED else "released"

    async def lock_identities(self, identity_ids: Sequence[UUID]) -> None:
        ordered = sorted(set(identity_ids))
        if not ordered:
            return
        await self.execute(
            select(IDENTITIES.c.id)
            .where(_any_of(IDENTITIES.c.id, ordered), IDENTITIES.c.space_id == self.space.id)
            .order_by(IDENTITIES.c.id)
            .with_for_update(key_share=True)
        )

    async def check_fence(self, fence: LeaseFence, *, lock: bool = True) -> None:
        i = IDENTITIES.c
        stmt = select(i.fencing_seq).where(i.id == fence.identity_id, i.space_id == self.space.id)
        if lock:
            stmt = stmt.with_for_update(of=IDENTITIES, key_share=True)
        current = (await self.execute(stmt)).scalar_one_or_none()
        if current is not None and current == fence.fencing:
            if fence.shared:
                owner = select(HISTORY.c.lease_id).where(
                    HISTORY.c.lease_id == fence.lease_id,
                    HISTORY.c.token == fence.token,
                    HISTORY.c.identity_id == fence.identity_id,
                    HISTORY.c.released_at.is_(None),
                )
            else:
                owner = select(LEASES.c.lease_id).where(
                    LEASES.c.identity_id == fence.identity_id,
                    LEASES.c.lease_id == fence.lease_id,
                    LEASES.c.token == fence.token,
                    LEASES.c.expires_at > func.now(),
                )
            if (await self.execute(owner)).scalar_one_or_none() is not None:
                return
        raise LeaseOwnershipLost(
            fence.lease_id, "fence", identity_id=fence.identity_id, fencing=fence.fencing
        )

    async def current_fencing(self, identity_id: UUID, *, lock: bool = False) -> int | None:
        stmt = select(IDENTITIES.c.fencing_seq).where(
            IDENTITIES.c.id == identity_id, IDENTITIES.c.space_id == self.space.id
        )
        if lock:
            stmt = stmt.with_for_update(read=True)
        value: int | None = (await self.execute(stmt)).scalar_one_or_none()
        return value

    async def revoke_lease(self, identity_id: UUID) -> LeaseRow | None:
        row = (
            (
                await self.execute(
                    update(LEASES)
                    .where(
                        LEASES.c.identity_id == identity_id,
                        LEASES.c.space_id == self.space.id,
                        LEASES.c.expires_at > func.now(),
                    )
                    .values(expires_at=func.now(), idempotency_key=None)
                    .returning(*LEASES.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        await self.execute(
            update(IDENTITIES)
            .where(IDENTITIES.c.id == identity_id)
            .values(fencing_seq=IDENTITIES.c.fencing_seq + 1)
        )
        await self.execute(
            update(HISTORY)
            .where(HISTORY.c.lease_id == row["lease_id"], HISTORY.c.released_at.is_(None))
            .values(released_at=func.now(), end_reason=LeaseEndReason.REVOKED)
        )
        return _lease_row(row)

    async def get_credentials(self, identity_id: UUID) -> EncryptedPayload | None:
        found = await self.get_credential(identity_id, "default", "default")
        return found[0] if found is not None else None

    async def get_session(self, identity_id: UUID, slot: str) -> tuple[EncryptedPayload, str] | None:
        c = SESSIONS.c
        row = (
            await self.execute(
                select(c.key_id, c.wrapped_dek, c.nonce, c.ciphertext, c.status).where(
                    c.identity_id == identity_id, c.slot == slot, c.space_id == self.space.id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return EncryptedPayload(row.key_id, row.wrapped_dek, row.nonce, row.ciphertext), row.status

    async def access_metadata(
        self, identity_id: UUID
    ) -> tuple[CredentialMetadataRow, list[SessionMetadataRow]]:
        credential_row = (
            await self.execute(
                select(CREDENTIALS.c.version, CREDENTIALS.c.updated_at).where(
                    CREDENTIALS.c.identity_id == identity_id,
                    CREDENTIALS.c.space_id == self.space.id,
                    CREDENTIALS.c.kind == "default",
                    CREDENTIALS.c.name == "default",
                )
            )
        ).one_or_none()
        credential = CredentialMetadataRow(
            credential_row is not None,
            credential_row.version if credential_row is not None else None,
            credential_row.updated_at if credential_row is not None else None,
        )
        rows = await self.execute(
            select(
                SESSIONS.c.slot,
                SESSIONS.c.status,
                SESSIONS.c.expires_at,
                SESSIONS.c.version,
                SESSIONS.c.written_by_fencing,
                SESSIONS.c.updated_at,
            )
            .where(SESSIONS.c.identity_id == identity_id, SESSIONS.c.space_id == self.space.id)
            .order_by(SESSIONS.c.slot)
        )
        return credential, [
            SessionMetadataRow(
                row.slot,
                row.status,
                row.expires_at,
                row.version,
                row.written_by_fencing,
                row.updated_at,
            )
            for row in rows
        ]

    # --------------------------------------------------------- квоты

    async def charge_quotas(
        self,
        identity_id: UUID,
        charges: Sequence[QuotaCharge],
        *,
        idempotency: IdempotencyRequest | None = None,
    ) -> ChargeOutcome:
        savepoint = await self.session.begin_nested()
        try:
            claim: IdempotencyClaim | None = None
            if idempotency is not None:
                claim = await self.claim_idempotency_key(idempotency)
                if not claim.claimed:
                    await savepoint.rollback()
                    return ChargeOutcome(duplicate=True, claim=claim)
            reached: list[QuotaWindow] = []
            for charge in charges:
                window_start = func.date_bin(
                    timedelta(seconds=charge.window_seconds),
                    func.now(),
                    sa_cast(charge.origin, DateTime(timezone=True)),
                )
                stmt = insert(QUOTAS).values(
                    identity_id=identity_id,
                    kind=charge.kind,
                    window_seconds=charge.window_seconds,
                    window_start=window_start,
                    space_id=self.space.id,
                    used=1,
                )
                upsert = stmt.on_conflict_do_update(
                    index_elements=[
                        QUOTAS.c.identity_id,
                        QUOTAS.c.kind,
                        QUOTAS.c.window_seconds,
                        QUOTAS.c.window_start,
                    ],
                    set_={"used": QUOTAS.c.used + 1},
                    where=QUOTAS.c.used < charge.limit,
                ).returning(QUOTAS.c.used, QUOTAS.c.window_start)
                row = (await self.execute(upsert)).one_or_none()
                if row is None:
                    start = (await self.execute(select(window_start))).scalar_one()
                    await savepoint.rollback()
                    return ChargeOutcome(
                        exhausted=QuotaWindow(charge.kind, charge.window_seconds, start, charge.limit)
                    )
                if row.used >= charge.limit:
                    reached.append(
                        QuotaWindow(charge.kind, charge.window_seconds, row.window_start, row.used)
                    )
            await savepoint.commit()
            return ChargeOutcome(reached=tuple(reached), claim=claim)
        except BaseException:
            if savepoint.is_active:
                await savepoint.rollback()
            raise

    async def bump_pacing(self, identity_id: UUID, charge: QuotaCharge) -> bool:
        window_start = func.date_bin(
            timedelta(seconds=charge.window_seconds),
            func.now(),
            sa_cast(charge.origin, DateTime(timezone=True)),
        )
        stmt = insert(QUOTAS).values(
            identity_id=identity_id,
            kind=charge.kind,
            window_seconds=charge.window_seconds,
            window_start=window_start,
            space_id=self.space.id,
            used=1,
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[
                QUOTAS.c.identity_id,
                QUOTAS.c.kind,
                QUOTAS.c.window_seconds,
                QUOTAS.c.window_start,
            ],
            set_={"used": QUOTAS.c.used + 1},
        ).returning(QUOTAS.c.used, QUOTAS.c.window_start)
        row = (await self.execute(upsert)).one()
        if row.used < charge.limit:
            return False
        await self.execute(
            update(QUOTAS)
            .where(
                QUOTAS.c.identity_id == identity_id,
                QUOTAS.c.kind == charge.kind,
                QUOTAS.c.window_seconds == charge.window_seconds,
                QUOTAS.c.window_start == row.window_start,
            )
            .values(used=0)
        )
        return True

    async def quota_usage(self, identity_id: UUID) -> list[QuotaWindow]:
        rows = await self.execute(
            select(QUOTAS.c.kind, QUOTAS.c.window_seconds, QUOTAS.c.window_start, QUOTAS.c.used)
            .where(
                QUOTAS.c.identity_id == identity_id,
                QUOTAS.c.space_id == self.space.id,
                QUOTAS.c.window_start + func.make_interval(0, 0, 0, 0, 0, 0, QUOTAS.c.window_seconds)
                > func.now(),
            )
            .order_by(QUOTAS.c.kind, QUOTAS.c.window_seconds)
        )
        return [QuotaWindow(r.kind, r.window_seconds, r.window_start, r.used) for r in rows]

    # --------------------------------------------------------- идемпотентность (IMP-03)

    async def claim_idempotency_key(self, request: IdempotencyRequest) -> IdempotencyClaim:
        keys = OPERATION_KEYS.c
        stmt = insert(OPERATION_KEYS).values(
            namespace=request.namespace,
            key=request.key,
            identity_id=request.owner_identity_id,
            space_id=self.space.id,
            operation=request.operation,
            fingerprint_version=FINGERPRINT_VERSION,
            fingerprint=request.fingerprint,
            fingerprint_key_id=request.fingerprint_key_id,
            attempt=1,
            outcome=None,
            expires_at=func.now() + request.ttl,
        )
        expired = keys.expires_at <= func.now()
        same_command = and_(
            keys.operation == stmt.excluded.operation,
            or_(
                keys.fingerprint_version == LEGACY_FINGERPRINT_VERSION,
                keys.fingerprint.in_(request.candidates),  # прежний ключ отпечатков — тот же отпечаток
            ),
        )
        # Конкурент с тем же ключом ждёт на конфликте до коммита победителя; его откат освобождает ключ.
        upsert = stmt.on_conflict_do_update(
            index_elements=[keys.namespace, keys.key],
            set_={
                "identity_id": stmt.excluded.identity_id,
                "space_id": stmt.excluded.space_id,
                "operation": stmt.excluded.operation,
                "fingerprint_version": stmt.excluded.fingerprint_version,
                "fingerprint": stmt.excluded.fingerprint,
                "fingerprint_key_id": stmt.excluded.fingerprint_key_id,
                "attempt": case((expired, 1), else_=keys.attempt + 1),
                "outcome": None,
                "result_type": None,
                "result_version": None,
                "result": None,
                "created_at": case((expired, func.now()), else_=keys.created_at),
                "expires_at": stmt.excluded.expires_at,
            },
            where=or_(expired, and_(keys.outcome == "failed", same_command)),
        ).returning(keys.attempt)
        for _ in range(3):
            attempt = (await self.execute(upsert)).scalar_one_or_none()
            if attempt is not None:
                return IdempotencyClaim(
                    True, attempt, "failed" if attempt > 1 else None, request.owner_identity_id
                )
            row = (
                await self.execute(
                    select(
                        keys.operation,
                        keys.fingerprint_version,
                        keys.fingerprint,
                        keys.fingerprint_key_id,
                        keys.attempt,
                        keys.outcome,
                        keys.identity_id,
                        keys.result_type,
                        keys.result_version,
                        keys.result,
                    )
                    .where(keys.namespace == request.namespace, keys.key == request.key)
                    # повтор держит строку ключа до конца транзакции: команды с одним ключом идут по очереди
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:  # запись удалена чисткой между двумя запросами — занять заново
                continue
            if row.operation != request.operation or (
                row.fingerprint_version != LEGACY_FINGERPRINT_VERSION
                and row.fingerprint != request.fingerprint_for(row.fingerprint_key_id)
            ):
                raise IdempotencyConflict(row.operation)
            return IdempotencyClaim(
                False,
                row.attempt,
                row.outcome,
                row.identity_id,
                row.result_type,
                row.result_version,
                row.result,
            )
        raise ConcurrentModification("idempotency_key", request.operation)

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
        await self.execute(
            update(OPERATION_KEYS)
            .where(
                OPERATION_KEYS.c.namespace == namespace,
                OPERATION_KEYS.c.key == key,
                OPERATION_KEYS.c.space_id == self.space.id,
            )
            .values(
                outcome=outcome,
                result_type=result_type,
                result_version=result_version,
                result=dict(result) if result is not None else None,
            )
        )

    # --------------------------------------------------------- здоровье аккаунта

    async def add_error_score(self, identity_id: UUID, delta: float) -> float:
        score: float = (
            await self.execute(
                update(IDENTITIES)
                .where(IDENTITIES.c.id == identity_id, IDENTITIES.c.space_id == self.space.id)
                .values(error_score=func.greatest(0.0, IDENTITIES.c.error_score + delta))
                .returning(IDENTITIES.c.error_score)
            )
        ).scalar_one()
        return score

    async def decay_error_score(self, identity_id: UUID, factor: float) -> None:
        await self.execute(
            update(IDENTITIES)
            .where(
                IDENTITIES.c.id == identity_id,
                IDENTITIES.c.space_id == self.space.id,
                IDENTITIES.c.error_score > 0,
            )
            .values(
                error_score=case(
                    (IDENTITIES.c.error_score * factor < 0.01, 0.0), else_=IDENTITIES.c.error_score * factor
                )
            )
        )

    async def set_session_status(self, identity_id: UUID, slot: str, status: str) -> None:
        await self.execute(
            update(SESSIONS)
            .where(
                SESSIONS.c.identity_id == identity_id,
                SESSIONS.c.slot == slot,
                SESSIONS.c.space_id == self.space.id,
            )
            .values(status=status, updated_at=func.now())
        )

    # --------------------------------------------------------- прокси

    async def insert_proxy(self, row: NewProxy) -> tuple[UUID, bool]:
        if row.aliases:
            known = await self.proxy_by_fingerprint(row.aliases)
            if known is not None:
                return known, False
        stmt = (
            insert(PROXIES)
            .values(
                id=row.id,
                space_id=self.space.id,
                pool=row.pool,
                scheme=row.scheme,
                host=row.host,
                port=row.port,
                fingerprint=row.fingerprint,
                fingerprint_key_id=row.fingerprint_key_id,
                max_identities=row.max_identities,
                **row.secret.columns(),
            )
            .on_conflict_do_nothing(index_elements=[PROXIES.c.space_id, PROXIES.c.fingerprint])
            .returning(PROXIES.c.id)
        )
        created: UUID | None = (await self.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return created, True
        existing: UUID = (
            await self.execute(
                select(PROXIES.c.id).where(
                    PROXIES.c.space_id == self.space.id, PROXIES.c.fingerprint == row.fingerprint
                )
            )
        ).scalar_one()
        return existing, False

    async def get_proxy(self, proxy_id: UUID) -> ProxyRow | None:
        row = (
            (
                await self.execute(
                    select(PROXIES).where(PROXIES.c.id == proxy_id, PROXIES.c.space_id == self.space.id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return _proxy_row(row) if row else None

    async def update_proxy(
        self, proxy_id: UUID, values: dict[str, Any], expected: dict[str, Any] | None = None
    ) -> bool:
        stmt = (
            update(PROXIES)
            .where(PROXIES.c.id == proxy_id, PROXIES.c.space_id == self.space.id)
            .values(**values)
        )
        for column, value in (expected or {}).items():
            stmt = stmt.where(PROXIES.c[column].is_not_distinct_from(value))
        return bool((await self.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def add_proxy_error(self, proxy_id: UUID, weight: float) -> float | None:
        score = (
            await self.execute(
                update(PROXIES)
                .where(
                    PROXIES.c.id == proxy_id,
                    PROXIES.c.space_id == self.space.id,
                    PROXIES.c.status == ProxyStatus.ALIVE,
                )
                # сложение в SQL: параллельные сообщения об ошибках не затирают друг друга (R2-F23)
                .values(error_score=PROXIES.c.error_score + weight)
                .returning(PROXIES.c.error_score)
            )
        ).scalar_one_or_none()
        return float(score) if score is not None else None

    async def proxy_by_fingerprint(self, fingerprints: Sequence[str]) -> UUID | None:
        if not fingerprints:
            return None
        found: UUID | None = (
            await self.execute(
                select(PROXIES.c.id)
                .where(
                    PROXIES.c.space_id == self.space.id, _any_of(PROXIES.c.fingerprint, list(fingerprints))
                )
                .order_by(PROXIES.c.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return found

    async def set_proxy_secret(
        self, proxy_id: UUID, fingerprint: str, secret: EncryptedPayload, *, fingerprint_key_id: str | None
    ) -> bool:
        result = await self.execute(
            update(PROXIES)
            .where(PROXIES.c.id == proxy_id, PROXIES.c.space_id == self.space.id)
            .values(fingerprint=fingerprint, fingerprint_key_id=fingerprint_key_id, **secret.columns())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def set_proxy_fingerprint(self, proxy_id: UUID, fingerprint: str, key_id: str | None) -> bool:
        taken = await self.proxy_by_fingerprint([fingerprint])
        if taken is not None and taken != proxy_id:
            return False
        result = await self.execute(
            update(PROXIES)
            .where(PROXIES.c.id == proxy_id, PROXIES.c.space_id == self.space.id)
            .values(fingerprint=fingerprint, fingerprint_key_id=key_id)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def delete_proxy(self, proxy_id: UUID) -> list[UUID] | None:
        owned = (
            await self.execute(
                select(PROXIES.c.id)
                .where(PROXIES.c.id == proxy_id, PROXIES.c.space_id == self.space.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if owned is None:
            return None
        unbound = list(
            (
                await self.execute(
                    delete(BINDINGS)
                    .where(BINDINGS.c.proxy_id == proxy_id, BINDINGS.c.space_id == self.space.id)
                    .returning(BINDINGS.c.identity_id)
                )
            ).scalars()
        )
        await self.execute(delete(PROXIES).where(PROXIES.c.id == proxy_id))
        return unbound

    async def get_binding(self, identity_id: UUID) -> ProxyRow | None:
        row = (
            (
                await self.execute(
                    select(PROXIES)
                    .join(BINDINGS, BINDINGS.c.proxy_id == PROXIES.c.id)
                    .where(BINDINGS.c.identity_id == identity_id, BINDINGS.c.space_id == self.space.id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return _proxy_row(row) if row else None

    async def choose_proxy(self, pool: str, max_identities: int | None, *, claim: bool) -> ProxyRow | None:
        capacity = PROXIES.c.max_identities if max_identities is None else literal(max_identities)
        alive = [
            PROXIES.c.space_id == self.space.id,
            PROXIES.c.pool == pool,
            PROXIES.c.status == ProxyStatus.ALIVE,
            PROXIES.c.bound_count < capacity,
        ]
        candidates = list(
            (
                await self.execute(
                    select(PROXIES.c.id)
                    .where(*alive)
                    .order_by(PROXIES.c.bound_count, PROXIES.c.error_score, PROXIES.c.id)
                    .limit(20)
                )
            ).scalars()
        )
        for proxy_id in candidates:
            if not claim:
                return await self.get_proxy(proxy_id)
            row = (
                (
                    await self.execute(
                        update(PROXIES)
                        .where(PROXIES.c.id == proxy_id, *alive)
                        .values(bound_count=PROXIES.c.bound_count + 1)
                        .returning(*PROXIES.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is not None:
                return _proxy_row(row)
        return None

    async def bind_proxy(self, identity_id: UUID, proxy_id: UUID, previous: UUID | None) -> None:
        stmt = insert(BINDINGS).values(identity_id=identity_id, space_id=self.space.id, proxy_id=proxy_id)
        stmt = stmt.on_conflict_do_update(
            index_elements=[BINDINGS.c.identity_id], set_={"proxy_id": proxy_id, "bound_at": func.now()}
        )
        await self.execute(stmt)
        if previous is not None and previous != proxy_id:
            await self.execute(
                update(PROXIES)
                .where(PROXIES.c.id == previous, PROXIES.c.bound_count > 0)
                .values(bound_count=PROXIES.c.bound_count - 1)
            )

    async def rebind_proxy(self, identity_id: UUID, proxy_id: UUID) -> tuple[UUID | None, ProxyRow] | None:
        owned = (
            await self.execute(
                select(IDENTITIES.c.id)
                .where(IDENTITIES.c.id == identity_id, IDENTITIES.c.space_id == self.space.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if owned is None:
            return None
        previous = (
            await self.execute(
                select(BINDINGS.c.proxy_id).where(
                    BINDINGS.c.identity_id == identity_id,
                    BINDINGS.c.space_id == self.space.id,
                )
            )
        ).scalar_one_or_none()
        if previous == proxy_id:
            current = await self.get_proxy(proxy_id)
            return (previous, current) if current is not None else None
        claimed = (
            (
                await self.execute(
                    update(PROXIES)
                    .where(
                        PROXIES.c.id == proxy_id,
                        PROXIES.c.space_id == self.space.id,
                        PROXIES.c.status == ProxyStatus.ALIVE,
                        PROXIES.c.bound_count < PROXIES.c.max_identities,
                    )
                    .values(bound_count=PROXIES.c.bound_count + 1)
                    .returning(*PROXIES.c)
                )
            )
            .mappings()
            .one_or_none()
        )
        if claimed is None:
            return None
        await self.bind_proxy(identity_id, proxy_id, previous)
        return previous, _proxy_row(claimed)

    async def adjust_proxy_load(self, proxy_id: UUID, delta: int) -> None:
        await self.execute(
            update(PROXIES)
            .where(PROXIES.c.id == proxy_id)
            .values(bound_count=func.greatest(0, PROXIES.c.bound_count + delta))
        )

    async def due_proxies(self, limit: int, *, pool: str | None = None) -> list[UUID]:
        p = PROXIES.c
        conditions = [
            p.space_id == self.space.id,
            or_(
                and_(p.status == ProxyStatus.QUARANTINED, p.next_check_at <= func.now()),
                # dead возвращается на редкую автоматическую перепроверку (R2-F04)
                and_(
                    p.status == ProxyStatus.DEAD,
                    or_(p.next_check_at.is_(None), p.next_check_at <= func.now()),
                ),
            ),
            or_(p.pinned_until.is_(None), p.pinned_until <= func.now()),
        ]
        if pool is not None:
            conditions.append(p.pool == pool)
        rows = await self.execute(
            select(p.id).where(*conditions).order_by(p.next_check_at.asc().nulls_first(), p.id).limit(limit)
        )
        return list(rows.scalars())

    async def proxy_read_page(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
        pool: str | None,
        status: str | None,
        capacity: str | None,
    ) -> list[ProxyReadRow]:
        binding_counts = (
            select(BINDINGS.c.proxy_id, func.count().label("bound_count"))
            .where(BINDINGS.c.space_id == self.space.id)
            .group_by(BINDINGS.c.proxy_id)
            .subquery()
        )
        lease_counts = (
            select(HISTORY.c.proxy_id, func.count().label("in_lease_count"))
            .where(
                HISTORY.c.space_id == self.space.id,
                HISTORY.c.proxy_id.is_not(None),
                HISTORY.c.released_at.is_(None),
            )
            .group_by(HISTORY.c.proxy_id)
            .subquery()
        )
        bound = func.coalesce(binding_counts.c.bound_count, 0)
        stmt = (
            select(
                PROXIES.c.id,
                PROXIES.c.pool,
                PROXIES.c.scheme,
                PROXIES.c.host,
                PROXIES.c.port,
                PROXIES.c.status,
                PROXIES.c.error_score,
                PROXIES.c.fail_streak,
                PROXIES.c.next_check_at,
                PROXIES.c.status_changed_at,
                PROXIES.c.status_source,
                PROXIES.c.checked_at,
                PROXIES.c.pinned_until,
                PROXIES.c.pending_observation,
                PROXIES.c.max_identities,
                bound.label("bound_count"),
                func.coalesce(lease_counts.c.in_lease_count, 0).label("in_lease_count"),
                PROXIES.c.created_at,
            )
            .outerjoin(binding_counts, binding_counts.c.proxy_id == PROXIES.c.id)
            .outerjoin(lease_counts, lease_counts.c.proxy_id == PROXIES.c.id)
            .where(PROXIES.c.space_id == self.space.id)
        )
        if pool is not None:
            stmt = stmt.where(PROXIES.c.pool == pool)
        if status is not None:
            stmt = stmt.where(PROXIES.c.status == status)
        if capacity == "available":
            stmt = stmt.where(bound < PROXIES.c.max_identities)
        elif capacity == "full":
            stmt = stmt.where(bound >= PROXIES.c.max_identities)
        elif capacity == "unbound":
            stmt = stmt.where(bound == 0)
        if before is not None:
            created_at, proxy_id = before
            stmt = stmt.where(
                or_(
                    PROXIES.c.created_at < created_at,
                    and_(PROXIES.c.created_at == created_at, PROXIES.c.id < proxy_id),
                )
            )
        rows = (
            await self.execute(stmt.order_by(PROXIES.c.created_at.desc(), PROXIES.c.id.desc()).limit(limit))
        ).mappings()
        return [ProxyReadRow(**dict(row)) for row in rows]

    async def proxy_bound_identities(self, proxy_id: UUID) -> list[ProxyBoundIdentityRow]:
        rows = (
            await self.execute(
                select(
                    BINDINGS.c.identity_id,
                    IDENTITIES.c.service,
                    IDENTITIES.c.external_ref,
                    BINDINGS.c.bound_at,
                )
                .join(
                    IDENTITIES,
                    and_(
                        IDENTITIES.c.id == BINDINGS.c.identity_id,
                        IDENTITIES.c.space_id == BINDINGS.c.space_id,
                    ),
                )
                .where(BINDINGS.c.space_id == self.space.id, BINDINGS.c.proxy_id == proxy_id)
                .order_by(BINDINGS.c.bound_at.desc(), BINDINGS.c.identity_id.desc())
            )
        ).mappings()
        return [ProxyBoundIdentityRow(**dict(row)) for row in rows]

    async def proxy_read_one(self, proxy_id: UUID) -> ProxyReadRow | None:
        bound_count = (
            select(func.count())
            .where(BINDINGS.c.space_id == self.space.id, BINDINGS.c.proxy_id == PROXIES.c.id)
            .scalar_subquery()
        )
        in_lease_count = (
            select(func.count())
            .where(
                HISTORY.c.space_id == self.space.id,
                HISTORY.c.proxy_id == PROXIES.c.id,
                HISTORY.c.released_at.is_(None),
            )
            .scalar_subquery()
        )
        row = (
            (
                await self.execute(
                    select(
                        PROXIES.c.id,
                        PROXIES.c.pool,
                        PROXIES.c.scheme,
                        PROXIES.c.host,
                        PROXIES.c.port,
                        PROXIES.c.status,
                        PROXIES.c.error_score,
                        PROXIES.c.fail_streak,
                        PROXIES.c.next_check_at,
                        PROXIES.c.status_changed_at,
                        PROXIES.c.status_source,
                        PROXIES.c.checked_at,
                        PROXIES.c.pinned_until,
                        PROXIES.c.pending_observation,
                        PROXIES.c.max_identities,
                        bound_count.label("bound_count"),
                        in_lease_count.label("in_lease_count"),
                        PROXIES.c.created_at,
                    ).where(PROXIES.c.space_id == self.space.id, PROXIES.c.id == proxy_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return ProxyReadRow(**dict(row)) if row is not None else None

    async def proxy_pool_capacities(self) -> list[ProxyPoolCapacityRow]:
        binding_counts = (
            select(BINDINGS.c.proxy_id, func.count().label("bound_count"))
            .where(BINDINGS.c.space_id == self.space.id)
            .group_by(BINDINGS.c.proxy_id)
            .subquery()
        )
        lease_counts = (
            select(HISTORY.c.proxy_id, func.count().label("in_lease_count"))
            .where(
                HISTORY.c.space_id == self.space.id,
                HISTORY.c.proxy_id.is_not(None),
                HISTORY.c.released_at.is_(None),
            )
            .group_by(HISTORY.c.proxy_id)
            .subquery()
        )
        rows = (
            await self.execute(
                select(
                    PROXIES.c.pool,
                    func.count().label("proxies"),
                    func.count().filter(PROXIES.c.status == "alive").label("alive"),
                    func.count().filter(PROXIES.c.status == "quarantined").label("quarantined"),
                    func.count().filter(PROXIES.c.status == "dead").label("dead"),
                    func.count()
                    .filter(PROXIES.c.status.not_in(("alive", "quarantined", "dead")))
                    .label("other"),
                    func.sum(PROXIES.c.max_identities).label("max_identities"),
                    func.sum(func.coalesce(binding_counts.c.bound_count, 0)).label("bound_count"),
                    func.sum(func.coalesce(lease_counts.c.in_lease_count, 0)).label("in_lease_count"),
                )
                .outerjoin(binding_counts, binding_counts.c.proxy_id == PROXIES.c.id)
                .outerjoin(lease_counts, lease_counts.c.proxy_id == PROXIES.c.id)
                .where(PROXIES.c.space_id == self.space.id)
                .group_by(PROXIES.c.pool)
                .order_by(PROXIES.c.pool)
            )
        ).mappings()
        return [ProxyPoolCapacityRow(**dict(row)) for row in rows]

    async def proxy_binding_history(
        self,
        proxy_id: UUID,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> list[ProxyBindingHistoryRow]:
        stmt = (
            select(
                EVENTS.c.event_id,
                EVENTS.c.identity_id,
                EVENTS.c.service,
                IDENTITIES.c.external_ref,
                EVENTS.c.data,
                EVENTS.c.actor,
                EVENTS.c.occurred_at,
            )
            .outerjoin(
                IDENTITIES,
                and_(
                    IDENTITIES.c.id == EVENTS.c.identity_id,
                    IDENTITIES.c.space_id == EVENTS.c.space_id,
                ),
            )
            .where(
                EVENTS.c.space_id == self.space.id,
                EVENTS.c.type == "proxy.binding.changed",
                or_(
                    EVENTS.c.proxy_id == proxy_id,
                    EVENTS.c.data["from_proxy_id"].astext == str(proxy_id),
                    EVENTS.c.data["to_proxy_id"].astext == str(proxy_id),
                ),
            )
        )
        if before is not None:
            occurred_at, event_id = before
            stmt = stmt.where(
                or_(
                    EVENTS.c.occurred_at < occurred_at,
                    and_(EVENTS.c.occurred_at == occurred_at, EVENTS.c.event_id < event_id),
                )
            )
        rows = (
            await self.execute(
                stmt.order_by(EVENTS.c.occurred_at.desc(), EVENTS.c.event_id.desc()).limit(limit)
            )
        ).mappings()
        result = []
        for row in rows:
            data = row["data"]
            result.append(
                ProxyBindingHistoryRow(
                    event_id=row["event_id"],
                    identity_id=row["identity_id"],
                    service=row["service"],
                    external_ref=row["external_ref"],
                    from_proxy_id=_optional_uuid(data.get("from_proxy_id")),
                    to_proxy_id=_optional_uuid(data.get("to_proxy_id")),
                    reason=str(data.get("reason") or "unknown"),
                    pool=str(data.get("pool") or ""),
                    actor=row["actor"],
                    occurred_at=row["occurred_at"],
                )
            )
        return result

    async def event_read_page(
        self,
        filters: EventReadFilter,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
    ) -> list[EventReadRow]:
        stmt = select(
            EVENTS.c.event_id,
            EVENTS.c.type,
            EVENTS.c.severity,
            EVENTS.c.service,
            EVENTS.c.identity_id,
            EVENTS.c.proxy_id,
            EVENTS.c.lease_id,
            EVENTS.c.sequence,
            EVENTS.c.actor,
            EVENTS.c.data,
            EVENTS.c.external,
            EVENTS.c.occurred_at,
            EVENTS.c.revision,
        ).where(
            EVENTS.c.space_id == self.space.id,
            EVENTS.c.type.in_(filters.allowed_types),
        )
        if filters.severities:
            stmt = stmt.where(EVENTS.c.severity.in_(filters.severities))
        if filters.service is not None:
            stmt = stmt.where(EVENTS.c.service == filters.service)
        if filters.identity_id is not None:
            stmt = stmt.where(EVENTS.c.identity_id == filters.identity_id)
        if filters.proxy_id is not None:
            stmt = stmt.where(EVENTS.c.proxy_id == filters.proxy_id)
        if filters.lease_id is not None:
            stmt = stmt.where(EVENTS.c.lease_id == filters.lease_id)
        if filters.actor is not None:
            stmt = stmt.where(EVENTS.c.actor.contains(filters.actor, autoescape=True))
        if filters.occurred_from is not None:
            stmt = stmt.where(EVENTS.c.occurred_at >= filters.occurred_from)
        if filters.occurred_to is not None:
            stmt = stmt.where(EVENTS.c.occurred_at <= filters.occurred_to)
        if before is not None:
            occurred_at, event_id = before
            stmt = stmt.where(
                or_(
                    EVENTS.c.occurred_at < occurred_at,
                    and_(EVENTS.c.occurred_at == occurred_at, EVENTS.c.event_id < event_id),
                )
            )
        rows = (
            await self.execute(
                stmt.order_by(EVENTS.c.occurred_at.desc(), EVENTS.c.event_id.desc()).limit(limit)
            )
        ).mappings()
        return [EventReadRow(**dict(row)) for row in rows]

    async def event_read_one(self, event_id: UUID) -> EventReadRow | None:
        row = (
            (
                await self.execute(
                    select(
                        EVENTS.c.event_id,
                        EVENTS.c.type,
                        EVENTS.c.severity,
                        EVENTS.c.service,
                        EVENTS.c.identity_id,
                        EVENTS.c.proxy_id,
                        EVENTS.c.lease_id,
                        EVENTS.c.sequence,
                        EVENTS.c.actor,
                        EVENTS.c.data,
                        EVENTS.c.external,
                        EVENTS.c.occurred_at,
                        EVENTS.c.revision,
                    ).where(EVENTS.c.space_id == self.space.id, EVENTS.c.event_id == event_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return EventReadRow(**dict(row)) if row is not None else None

    # --------------------------------------------------------- challenge

    async def open_challenge(
        self,
        challenge_id: UUID,
        identity_id: UUID,
        kind: str,
        hint: str,
        context: EncryptedPayload,
        expires_at: datetime | None,
    ) -> bool:
        stmt = (
            insert(CHALLENGES)
            .values(
                id=challenge_id,
                identity_id=identity_id,
                space_id=self.space.id,
                kind=kind,
                hint=hint,
                expires_at=expires_at,
                **context.columns("context_"),
            )
            .on_conflict_do_nothing()
        )
        return bool((await self.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def get_challenge(self, challenge_id: UUID) -> ChallengeRow | None:
        row = (
            (
                await self.execute(
                    select(CHALLENGES).where(
                        CHALLENGES.c.id == challenge_id, CHALLENGES.c.space_id == self.space.id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return _challenge_row(row) if row else None

    async def open_challenge_of(self, identity_id: UUID) -> ChallengeRow | None:
        row = (
            (
                await self.execute(
                    select(CHALLENGES).where(
                        CHALLENGES.c.identity_id == identity_id,
                        CHALLENGES.c.space_id == self.space.id,
                        CHALLENGES.c.resolved_at.is_(None),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return _challenge_row(row) if row else None

    async def answer_challenge(self, challenge_id: UUID, nonce: bytes, ciphertext: bytes) -> bool:
        result = await self.execute(
            update(CHALLENGES)
            .where(
                CHALLENGES.c.id == challenge_id,
                CHALLENGES.c.space_id == self.space.id,
                CHALLENGES.c.resolved_at.is_(None),
            )
            .values(answer_nonce=nonce, answer_ciphertext=ciphertext, answered_at=func.now())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def resolve_challenge(self, challenge_id: UUID) -> bool:
        result = await self.execute(
            update(CHALLENGES)
            .where(CHALLENGES.c.id == challenge_id, CHALLENGES.c.resolved_at.is_(None))
            # ответ (SMS-код, пароль 2FA) после закрытия не нужен: шифртекст не хранится (R2-F20)
            .values(resolved_at=func.now(), answer_nonce=None, answer_ciphertext=None)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def expired_challenges(self, limit: int) -> list[ChallengeRow]:
        rows = await self.execute(
            select(CHALLENGES)
            .where(
                CHALLENGES.c.space_id == self.space.id,
                CHALLENGES.c.resolved_at.is_(None),
                CHALLENGES.c.expires_at <= func.now(),
            )
            .order_by(CHALLENGES.c.expires_at)
            .limit(limit)
        )
        return [_challenge_row(r) for r in rows.mappings()]

    # --------------------------------------------------------- верификации

    async def load_verification(self, identity_id: UUID, factor: str) -> VerificationRow | None:
        row = (
            (
                await self.execute(
                    select(VERIFICATIONS).where(
                        VERIFICATIONS.c.identity_id == identity_id,
                        VERIFICATIONS.c.factor == factor,
                        VERIFICATIONS.c.space_id == self.space.id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        evidence = (
            EncryptedPayload(
                row["evidence_key_id"],
                row["evidence_wrapped_dek"],
                row["evidence_nonce"],
                row["evidence_ciphertext"],
            )
            if row["evidence_ciphertext"] is not None
            else None
        )
        return VerificationRow(
            attempts=row["attempts"],
            started_at=row["started_at"],
            submitted_at=row["submitted_at"],
            verified_at=row["verified_at"],
            expires_at=row["expires_at"],
            evidence=evidence,
            via_identity_id=row["via_identity_id"],
            details=dict(row["details"]),
        )

    async def save_verification(
        self, identity_id: UUID, factor: str, values: dict[str, Any], evidence: EncryptedPayload | None = None
    ) -> None:
        data = dict(values)
        if evidence is not None:
            data |= evidence.columns("evidence_")
        stmt = insert(VERIFICATIONS).values(
            identity_id=identity_id, factor=factor, space_id=self.space.id, **data
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[VERIFICATIONS.c.identity_id, VERIFICATIONS.c.factor],
            set_=data or {"factor": factor},
        )
        await self.execute(stmt)

    async def due_regions(self, region: str, limit: int) -> list[tuple[UUID, str]]:
        rows = await self.execute(
            select(REGION_STATES.c.identity_id, REGION_STATES.c.key)
            .where(
                REGION_STATES.c.space_id == self.space.id,
                REGION_STATES.c.region == region,
                REGION_STATES.c.until <= func.now(),
            )
            .order_by(REGION_STATES.c.until)
            .limit(limit)
        )
        return [(r.identity_id, r.key) for r in rows]

    async def expired_factors(self, identity_id: UUID) -> list[str]:
        rs = REGION_STATES.c
        rows = await self.execute(
            select(rs.key).where(
                rs.identity_id == identity_id,
                rs.space_id == self.space.id,
                rs.region == "verification",
                rs.state == VerificationState.VERIFIED,
                rs.until <= func.now(),
            )
        )
        return sorted(rows.scalars())

    async def stale_identities(
        self, service: str, region: str, key: str, older_than_seconds: float, limit: int
    ) -> list[UUID]:
        rs = REGION_STATES.c
        synced = (
            select(rs.synced_at)
            .where(rs.identity_id == IDENTITIES.c.id, rs.region == region, rs.key == key)
            .correlate(IDENTITIES)
            .scalar_subquery()
        )
        threshold = func.now() - timedelta(seconds=older_than_seconds)
        rows = await self.execute(
            select(IDENTITIES.c.id)
            .where(
                IDENTITIES.c.space_id == self.space.id,
                IDENTITIES.c.service == service,
                # provisioning и retired пробу пройти не могут и только занимали бы очередь (R2-F02)
                IDENTITIES.c.lifecycle == Lifecycle.READY,
                or_(synced.is_(None), synced < threshold),
            )
            .order_by(
                IDENTITIES.c.probe_attempted_at.asc().nulls_first(),
                synced.asc().nulls_first(),
                IDENTITIES.c.id,
            )
            .limit(limit)
        )
        return list(rows.scalars())

    async def mark_probe_attempts(self, identity_ids: Sequence[UUID]) -> None:
        if not identity_ids:
            return
        await self.execute(
            update(IDENTITIES)
            .where(_any_of(IDENTITIES.c.id, identity_ids), IDENTITIES.c.space_id == self.space.id)
            # clock_timestamp: попытки одной транзакции получают то же время, разных — растущее
            .values(probe_attempted_at=func.clock_timestamp())
        )

    # --------------------------------------------------------- поиск

    async def search_identities(
        self,
        flt: SearchFilter,
        *,
        limit: int,
        after: tuple[Any, UUID] | None,
        count_limit: int | None,
    ) -> tuple[list[IdentityRow], int | None]:
        i = IDENTITIES.c
        conditions: list[Any] = [i.space_id == self.space.id]
        if flt.exact_id is not None:
            conditions.append(i.id == flt.exact_id)
        if flt.short_id is not None:
            # окончание id без дефисов: как `UUID.hex` в MemoryStore, длина окончания любая
            conditions.append(func.replace(sa_cast(i.id, Text), "-", "").endswith(flt.short_id))
        if flt.services:
            conditions.append(i.service.in_(sorted(flt.services)))
        for column, values in flt.regions.items():
            conditions.append(i[column].in_(sorted(values)))
        if flt.any_region:
            wanted = sorted(flt.any_region)
            conditions.append(
                or_(*(i[c].in_(wanted) for c in ("lifecycle", "standing", "auth_state", "availability")))
            )
        if flt.tags:
            conditions.append(i.tags.contains(sorted(flt.tags)))
        for column, variants in flt.contains:
            conditions.append(or_(*(i[column].contains(v) for v in variants)))
        for term in flt.terms:
            escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(i.search_text.ilike(f"%{escaped}%", escape="\\"))
        now = await self.now()
        live_lease = (
            select(literal(1))
            .where(
                LEASES.c.identity_id == i.id,
                LEASES.c.expires_at > now,
            )
            .exists()
        )
        currently_available = or_(
            i.availability == Availability.AVAILABLE,
            and_(
                i.availability.in_([Availability.PAUSED, Availability.COOLING]),
                i.availability_until.is_not(None),
                i.availability_until <= now,
            ),
        )
        eligible = and_(
            i.lifecycle == Lifecycle.READY,
            i.standing.in_(["good", "limited"]),
            i.auth_state == AuthState.AUTHENTICATED,
            currently_available,
        )
        if flt.leased is not None:
            conditions.append(live_lease if flt.leased else ~live_lease)
        if flt.attention is not None:
            open_challenge = (
                select(literal(1))
                .where(CHALLENGES.c.identity_id == i.id, CHALLENGES.c.resolved_at.is_(None))
                .exists()
            )
            attention = or_(
                i.auth_state == AuthState.CHALLENGE,
                func.jsonb_array_length(i.requirements) > 0,
                open_challenge,
            )
            conditions.append(attention if flt.attention else ~attention)
        if flt.eligible is not None:
            conditions.append(eligible if flt.eligible else ~eligible)
        if flt.has_override is not None:
            override = (
                select(literal(1))
                .where(
                    REGION_STATES.c.identity_id == i.id,
                    REGION_STATES.c.pinned_until.is_not(None),
                    REGION_STATES.c.pinned_until > now,
                )
                .exists()
            )
            conditions.append(override if flt.has_override else ~override)
        if flt.requirement is not None:
            conditions.append(i.requirements.contains([{"code": flt.requirement}]))
        if flt.factor is not None:
            conditions.append(i.verified_factors.contains([flt.factor]))
        if flt.factor_state is not None:
            factor_state = select(literal(1)).where(
                REGION_STATES.c.identity_id == i.id,
                REGION_STATES.c.region == "verification",
                REGION_STATES.c.state == flt.factor_state,
            )
            if flt.factor is not None:
                factor_state = factor_state.where(REGION_STATES.c.key == flt.factor)
            conditions.append(factor_state.exists())
        if flt.tier_factors:
            conditions.append(
                or_(
                    *(
                        and_(i.service == service, i.verified_factors.contains(sorted(factors)))
                        for service, factors in sorted(flt.tier_factors.items())
                    )
                )
            )
        if flt.proxy is not None:
            escaped_proxy = flt.proxy.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            proxy_match = (
                select(literal(1))
                .select_from(BINDINGS.join(PROXIES, BINDINGS.c.proxy_id == PROXIES.c.id))
                .where(
                    BINDINGS.c.identity_id == i.id,
                    or_(
                        PROXIES.c.host.ilike(f"%{escaped_proxy}%", escape="\\"),
                        sa_cast(PROXIES.c.id, Text).ilike(f"{escaped_proxy}%", escape="\\"),
                    ),
                )
                .exists()
            )
            conditions.append(proxy_match)
        if flt.linked is not None:
            linked = (
                select(literal(1))
                .where(or_(LINKS.c.identity_id == i.id, LINKS.c.linked_identity_id == i.id))
                .exists()
            )
            conditions.append(linked if flt.linked else ~linked)
        if flt.created_from is not None:
            conditions.append(i.created_at >= flt.created_from)
        if flt.created_to is not None:
            conditions.append(i.created_at < flt.created_to)
        if flt.last_leased_from is not None or flt.last_leased_to is not None:
            last_lease = select(literal(1)).where(HISTORY.c.identity_id == i.id)
            if flt.last_leased_from is not None:
                last_lease = last_lease.where(HISTORY.c.acquired_at >= flt.last_leased_from)
            if flt.last_leased_to is not None:
                last_lease = last_lease.where(HISTORY.c.acquired_at < flt.last_leased_to)
            conditions.append(last_lease.exists())
        if flt.synced_from is not None or flt.synced_to is not None:
            synced = select(literal(1)).where(
                REGION_STATES.c.identity_id == i.id,
                REGION_STATES.c.synced_at.is_not(None),
            )
            if flt.synced_from is not None:
                synced = synced.where(REGION_STATES.c.synced_at >= flt.synced_from)
            if flt.synced_to is not None:
                synced = synced.where(REGION_STATES.c.synced_at < flt.synced_to)
            conditions.append(synced.exists())
        if flt.standing_changed_from is not None or flt.standing_changed_to is not None:
            standing_changed = select(literal(1)).where(
                REGION_STATES.c.identity_id == i.id,
                REGION_STATES.c.region == "standing",
            )
            if flt.standing_changed_from is not None:
                standing_changed = standing_changed.where(
                    REGION_STATES.c.changed_at >= flt.standing_changed_from
                )
            if flt.standing_changed_to is not None:
                standing_changed = standing_changed.where(
                    REGION_STATES.c.changed_at < flt.standing_changed_to
                )
            conditions.append(standing_changed.exists())
        # строки сравниваются по кодам символов (COLLATE "C"): порядок не зависит от локали базы
        # и совпадает с MemoryStore (R2-P16)
        direct_sort = {
            "auth_state": i.auth_state.collate("C"),
            "availability": i.availability.collate("C"),
            "created_at": i.created_at,
            "error_score": i.error_score,
            "external_ref": i.external_ref.collate("C"),
            "lifecycle": i.lifecycle.collate("C"),
            "service": i.service.collate("C"),
            "standing": i.standing.collate("C"),
        }
        if flt.sort_by.startswith("profile:"):
            sort_expr = i.profile[flt.sort_by.partition(":")[2]].astext.collate("C")
        elif flt.sort_by == "eligible":
            sort_expr = case((eligible, 1), else_=0)
        elif flt.sort_by == "leased":
            sort_expr = case((live_lease, 1), else_=0)
        elif flt.sort_by == "last_leased_at":
            sort_expr = (
                select(func.max(HISTORY.c.acquired_at)).where(HISTORY.c.identity_id == i.id).scalar_subquery()
            )
        elif flt.sort_by == "synced_at":
            sort_expr = (
                select(func.max(REGION_STATES.c.synced_at))
                .where(REGION_STATES.c.identity_id == i.id)
                .scalar_subquery()
            )
        elif flt.sort_by == "verification_tier":
            tier_cases = [
                (
                    and_(i.service == service, i.verified_factors.contains(sorted(factors))),
                    index,
                )
                for service, tiers in sorted(flt.sort_tiers.items())
                for index, factors in reversed(tuple(enumerate(tiers)))
            ]
            sort_expr = case(*tier_cases, else_=None) if tier_cases else literal(None)
        else:
            sort_expr = direct_sort[flt.sort_by]
        page = select(IDENTITIES).where(*conditions)
        if after is not None:
            after_value, after_id = after
            if after_value is None:
                page = page.where(sort_expr.is_(None), i.id < after_id)
            else:
                comparison = sort_expr < after_value if flt.sort_desc else sort_expr > after_value
                page = page.where(
                    or_(
                        sort_expr.is_(None),
                        comparison,
                        and_(sort_expr == after_value, i.id < after_id),
                    )
                )
        ordering = sort_expr.desc().nulls_last() if flt.sort_desc else sort_expr.asc().nulls_last()
        page = page.order_by(ordering, i.id.desc()).limit(limit)
        rows = [
            IdentityRow(**{f: r[f] for f in IDENTITY_FIELDS}) for r in (await self.execute(page)).mappings()
        ]
        total = None
        if count_limit is not None:
            capped = select(i.id).where(*conditions).limit(count_limit + 1).subquery()
            total = int((await self.execute(select(func.count()).select_from(capped))).scalar_one())
        return rows, total

    # --------------------------------------------------------- логи аренды

    async def insert_lease_logs(self, rows: Sequence[dict[str, Any]]) -> None:
        if rows:
            await self.execute(insert(LEASE_LOGS).values([{**r, "space_id": self.space.id} for r in rows]))

    # --------------------------------------------------------- read-модели

    async def list_regions(self, identity_id: UUID) -> list[tuple[str, str, RegionState]]:
        rows = await self.execute(
            select(REGION_STATES)
            .where(REGION_STATES.c.identity_id == identity_id, REGION_STATES.c.space_id == self.space.id)
            .order_by(REGION_STATES.c.region, REGION_STATES.c.key)
        )
        result = []
        for r in rows.mappings():
            values = {f: r[f] for f in REGION_FIELDS}
            values["change_source"] = ChangeSource(values["change_source"])
            result.append((r["region"], r["key"], RegionState(**values)))
        return result

    async def current_lease(self, identity_id: UUID) -> LeaseRow | None:
        row = (
            (
                await self.execute(
                    select(LEASES).where(
                        LEASES.c.identity_id == identity_id,
                        LEASES.c.space_id == self.space.id,
                        LEASES.c.expires_at > func.now(),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return _lease_row(row) if row else None

    async def data_namespaces(self, identity_id: UUID) -> list[tuple[str, int, int, datetime]]:
        rows = await self.execute(
            select(DATA.c.namespace, DATA.c.schema_version, DATA.c.version, DATA.c.updated_at)
            .where(DATA.c.identity_id == identity_id, DATA.c.space_id == self.space.id)
            .order_by(DATA.c.namespace)
        )
        return [(r.namespace, r.schema_version, r.version, r.updated_at) for r in rows]

    async def region_counts(self, service: str | None) -> list[tuple[str, str, str, int]]:
        result: list[tuple[str, str, str, int]] = []
        for column in ("lifecycle", "standing", "auth_state", "availability"):
            col = IDENTITIES.c[column]
            stmt = select(IDENTITIES.c.service, col, func.count()).where(
                IDENTITIES.c.space_id == self.space.id
            )
            if service is not None:
                stmt = stmt.where(IDENTITIES.c.service == service)
            grouped: list[Any] = list(await self.execute(stmt.group_by(IDENTITIES.c.service, col)))
            result.extend((str(r[0]), column, str(r[1]), int(r[2])) for r in grouped)
        return result

    async def overview_snapshot(self) -> OverviewSnapshot:
        i = IDENTITIES.c
        now = await self.now()
        # некоррелированные подзапросы: множество считается один раз, а не на каждую строку (R2-D15)
        live_lease = i.id.in_(
            select(LEASES.c.identity_id).where(LEASES.c.space_id == self.space.id, LEASES.c.expires_at > now)
        )
        open_challenge = i.id.in_(
            select(CHALLENGES.c.identity_id).where(
                CHALLENGES.c.space_id == self.space.id, CHALLENGES.c.resolved_at.is_(None)
            )
        )
        currently_available = or_(
            i.availability == Availability.AVAILABLE,
            and_(
                i.availability.in_([Availability.PAUSED, Availability.COOLING]),
                i.availability_until.is_not(None),
                i.availability_until <= now,
            ),
        )
        eligible = and_(
            i.lifecycle == Lifecycle.READY,
            i.standing.in_(["good", "limited"]),
            i.auth_state == AuthState.AUTHENTICATED,
            currently_available,
        )
        heavy = or_(
            i.standing.in_(["frozen", "banned", "deleted"]),
            i.auth_state == AuthState.SESSION_INVALID,
            i.availability == Availability.QUARANTINED,
        )
        action_required = or_(
            i.auth_state == AuthState.CHALLENGE,
            func.jsonb_array_length(i.requirements) > 0,
            open_challenge,
        )
        summary_rows = (
            await self.execute(
                select(
                    i.service,
                    func.count().label("total"),
                    func.count().filter(eligible).label("eligible"),
                    func.count().filter(live_lease).label("leased"),
                    func.count().filter(heavy).label("heavy"),
                    func.count().filter(open_challenge).label("open_challenges"),
                    func.count().filter(action_required).label("action_required"),
                    func.count().filter(i.created_at >= now - timedelta(days=1)).label("added_24h"),
                    func.count().filter(i.created_at >= now - timedelta(days=7)).label("added_7d"),
                )
                .where(i.space_id == self.space.id)
                .group_by(i.service)
                .order_by(i.service)
            )
        ).mappings()
        services = tuple(
            OverviewServiceRow(
                service=str(row["service"]),
                total=int(row["total"]),
                eligible=int(row["eligible"]),
                leased=int(row["leased"]),
                heavy=int(row["heavy"]),
                open_challenges=int(row["open_challenges"]),
                action_required=int(row["action_required"]),
                added_24h=int(row["added_24h"]),
                added_7d=int(row["added_7d"]),
            )
            for row in summary_rows
        )

        factor_rows = await self.execute(
            select(i.service, i.verified_factors, func.count().label("count"))
            .where(i.space_id == self.space.id)
            .group_by(i.service, i.verified_factors)
            .order_by(i.service, i.verified_factors)
        )
        factor_sets = tuple(
            (
                str(row["service"]),
                tuple(sorted(row["verified_factors"])),
                int(row["count"]),
            )
            for row in factor_rows.mappings()
        )

        verification_rows = await self.execute(
            select(
                i.service,
                REGION_STATES.c.key,
                REGION_STATES.c.state,
                func.count().label("count"),
            )
            .select_from(REGION_STATES.join(IDENTITIES, i.id == REGION_STATES.c.identity_id))
            .where(i.space_id == self.space.id, REGION_STATES.c.region == "verification")
            .group_by(i.service, REGION_STATES.c.key, REGION_STATES.c.state)
            .order_by(i.service, REGION_STATES.c.key, REGION_STATES.c.state)
        )
        verification_states = tuple(
            (str(row["service"]), str(row["key"]), str(row["state"]), int(row["count"]))
            for row in verification_rows.mappings()
        )

        requirement_rows = await self.execute(
            select(i.service, i.requirements).where(
                i.space_id == self.space.id, func.jsonb_array_length(i.requirements) > 0
            )
        )
        requirement_counts: dict[tuple[str, str], int] = {}
        requirement_deadlines: dict[tuple[str, str], datetime] = {}
        for row in requirement_rows.mappings():
            service = row["service"]
            requirements = row["requirements"]
            seen: set[str] = set()
            for requirement in requirements:
                if not isinstance(requirement, dict) or not isinstance(requirement.get("code"), str):
                    continue
                code = requirement["code"]
                key = (str(service), code)
                if code not in seen:
                    requirement_counts[key] = requirement_counts.get(key, 0) + 1
                    seen.add(code)
                deadline = _requirement_deadline(requirement.get("deadline"))
                if deadline is not None and (
                    key not in requirement_deadlines or deadline < requirement_deadlines[key]
                ):
                    requirement_deadlines[key] = deadline

        return OverviewSnapshot(
            services=services,
            factor_sets=factor_sets,
            verification_states=verification_states,
            requirements=tuple(
                OverviewRequirementRow(service, code, count, requirement_deadlines.get((service, code)))
                for (service, code), count in sorted(requirement_counts.items())
            ),
        )

    async def attention_rows(self) -> list[AttentionIdentityRow]:
        identity = IDENTITIES
        open_challenge = (
            select(literal(1))
            .where(
                CHALLENGES.c.identity_id == identity.c.id,
                CHALLENGES.c.space_id == self.space.id,
                CHALLENGES.c.resolved_at.is_(None),
            )
            .exists()
        )
        attention_region = (
            select(literal(1))
            .where(
                REGION_STATES.c.identity_id == identity.c.id,
                REGION_STATES.c.space_id == self.space.id,
                or_(
                    and_(
                        REGION_STATES.c.region == "verification",
                        REGION_STATES.c.state == "under_review",
                    ),
                    and_(
                        REGION_STATES.c.pinned_until.is_not(None),
                        REGION_STATES.c.pinned_until > func.now(),
                        REGION_STATES.c.raw_status.is_not(None),
                        REGION_STATES.c.raw_status != REGION_STATES.c.state,
                    ),
                ),
            )
            .exists()
        )
        identities = list(
            (
                await self.execute(
                    select(
                        identity.c.id,
                        identity.c.service,
                        identity.c.external_ref,
                        identity.c.availability,
                        identity.c.error_score,
                        identity.c.requirements,
                        identity.c.updated_at,
                    ).where(
                        identity.c.space_id == self.space.id,
                        or_(
                            func.jsonb_array_length(identity.c.requirements) > 0,
                            identity.c.availability == Availability.QUARANTINED,
                            open_challenge,
                            attention_region,
                        ),
                    )
                )
            ).mappings()
        )
        if not identities:
            return []
        identity_ids = [row["id"] for row in identities]
        region_rows = await self.execute(
            select(REGION_STATES).where(
                REGION_STATES.c.space_id == self.space.id,
                _any_of(REGION_STATES.c.identity_id, identity_ids),
            )
        )
        regions: dict[UUID, list[tuple[str, str, RegionState]]] = {}
        for row in region_rows.mappings():
            values = {field: row[field] for field in REGION_FIELDS}
            values["change_source"] = ChangeSource(values["change_source"])
            regions.setdefault(row["identity_id"], []).append(
                (row["region"], row["key"], RegionState(**values))
            )
        challenge_rows = await self.execute(
            select(
                CHALLENGES.c.id,
                CHALLENGES.c.identity_id,
                CHALLENGES.c.kind,
                CHALLENGES.c.hint,
                CHALLENGES.c.created_at,
                CHALLENGES.c.expires_at,
            ).where(
                CHALLENGES.c.space_id == self.space.id,
                _any_of(CHALLENGES.c.identity_id, identity_ids),
                CHALLENGES.c.resolved_at.is_(None),
            )
        )
        challenges = {
            row.identity_id: AttentionChallengeRow(row.id, row.kind, row.hint, row.created_at, row.expires_at)
            for row in challenge_rows
        }
        return [
            AttentionIdentityRow(
                id=row["id"],
                service=row["service"],
                external_ref=row["external_ref"],
                availability=row["availability"],
                error_score=row["error_score"],
                requirements=tuple(item for item in row["requirements"] if isinstance(item, dict)),
                regions=tuple(regions.get(row["id"], [])),
                challenge=challenges.get(row["id"]),
                updated_at=row["updated_at"],
            )
            for row in identities
        ]

    async def lease_history(
        self, identity_id: UUID, *, limit: int, before: datetime | None
    ) -> list[LeaseHistoryRow]:
        stmt = select(HISTORY).where(
            HISTORY.c.identity_id == identity_id, HISTORY.c.space_id == self.space.id
        )
        if before is not None:
            stmt = stmt.where(HISTORY.c.acquired_at < before)
        rows = await self.execute(stmt.order_by(HISTORY.c.acquired_at.desc()).limit(limit))
        return [LeaseHistoryRow(**{f: r[f] for f in LEASE_HISTORY_FIELDS}) for r in rows.mappings()]

    async def lease_stats(self, identity_id: UUID, since: datetime) -> LeaseStatsRow:
        base = [
            HISTORY.c.identity_id == identity_id,
            HISTORY.c.space_id == self.space.id,
            HISTORY.c.acquired_at >= since,
        ]
        held = func.coalesce(HISTORY.c.duration, func.now() - HISTORY.c.acquired_at)
        agg: Any = (
            await self.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(held), timedelta(0)),
                    func.avg(held),
                    func.max(held),
                    func.coalesce(func.sum(HISTORY.c.succeeded), 0),
                    func.coalesce(func.sum(HISTORY.c.failed), 0),
                ).where(*base)
            )
        ).one()
        reasons: list[Any] = list(
            await self.execute(
                select(func.coalesce(HISTORY.c.end_reason, "active"), func.count())
                .where(*base)
                .group_by(HISTORY.c.end_reason)
            )
        )
        ops: list[Any] = list(
            await self.execute(
                select(text("key"), func.sum(text("value::bigint")))
                .select_from(
                    HISTORY.join(
                        func.jsonb_each_text(HISTORY.c.operations).table_valued("key", "value"), literal(True)
                    )
                )
                .where(*base)
                .group_by(text("key"))
            )
        )
        return LeaseStatsRow(
            count=int(agg[0]),
            total=agg[1],
            average=agg[2],
            longest=agg[3],
            by_reason={str(r[0]): int(r[1]) for r in reasons},
            operations={str(r[0]): int(r[1]) for r in ops},
            succeeded=int(agg[4]),
            failed=int(agg[5]),
        )

    def _lease_read_statement(self) -> Select[Any]:
        # heartbeat эксклюзивной аренды пишется в ih_leases, в историю — раз в stats_flush (D-210)
        columns = [
            func.coalesce(LEASES.c.heartbeat_at, HISTORY.c.last_heartbeat_at).label(field)
            if field == "last_heartbeat_at"
            else HISTORY.c[field]
            for field in LEASE_HISTORY_FIELDS
        ]
        return select(
            *columns,
            IDENTITIES.c.service,
            IDENTITIES.c.external_ref,
            LEASES.c.expires_at,
        ).select_from(
            HISTORY.join(
                IDENTITIES,
                and_(
                    IDENTITIES.c.id == HISTORY.c.identity_id,
                    IDENTITIES.c.space_id == HISTORY.c.space_id,
                ),
            ).outerjoin(
                LEASES,
                and_(
                    LEASES.c.lease_id == HISTORY.c.lease_id,
                    LEASES.c.space_id == HISTORY.c.space_id,
                ),
            )
        )

    @staticmethod
    def _lease_read_row(row: Any) -> LeaseReadRow:
        return LeaseReadRow(
            **{field: row[field] for field in LEASE_HISTORY_FIELDS},
            service=row["service"],
            external_ref=row["external_ref"],
            expires_at=row["expires_at"],
        )

    async def lease_read_rows(self, since: datetime) -> list[LeaseReadRow]:
        stmt = self._lease_read_statement().where(
            HISTORY.c.space_id == self.space.id,
            or_(
                HISTORY.c.acquired_at >= since,
                HISTORY.c.released_at.is_(None),
                HISTORY.c.released_at >= since,
            ),
        )
        rows = (await self.execute(stmt.order_by(HISTORY.c.acquired_at.desc()))).mappings()
        return [self._lease_read_row(row) for row in rows]

    async def lease_read_page(
        self,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None,
        pool: str | None,
        service: str | None,
        holder: str | None,
    ) -> list[LeaseReadRow]:
        stmt = self._lease_read_statement().where(HISTORY.c.space_id == self.space.id)
        if pool is not None:
            stmt = stmt.where(HISTORY.c.pool == pool)
        if service is not None:
            stmt = stmt.where(IDENTITIES.c.service == service)
        if holder is not None:
            stmt = stmt.where(HISTORY.c.holder == holder)
        if before is not None:
            stmt = stmt.where(tuple_(HISTORY.c.acquired_at, HISTORY.c.lease_id) < before)
        rows = (
            await self.execute(
                stmt.order_by(HISTORY.c.acquired_at.desc(), HISTORY.c.lease_id.desc()).limit(limit)
            )
        ).mappings()
        return [self._lease_read_row(row) for row in rows]

    async def identities_page(self, service: str, after: UUID | None, limit: int) -> list[IdentityRow]:
        stmt = select(IDENTITIES).where(
            IDENTITIES.c.space_id == self.space.id, IDENTITIES.c.service == service
        )
        if after is not None:
            stmt = stmt.where(IDENTITIES.c.id > after)
        rows = await self.execute(stmt.order_by(IDENTITIES.c.id).limit(limit))
        return [IdentityRow(**{f: r[f] for f in IDENTITY_FIELDS}) for r in rows.mappings()]

    # --------------------------------------------------------- исходящие вебхуки

    async def insert_webhook_endpoint(self, row: NewWebhookEndpoint) -> WebhookEndpointRow:
        values = {
            "id": row.id,
            "space_id": self.space.id,
            "url": row.url,
            "description": row.description,
            "event_types": row.event_types,
            "filter": row.filter,
            "min_severity": row.min_severity,
            **row.secret.columns(),
        }
        stored = (
            (await self.execute(insert(WEBHOOK_ENDPOINTS).values(**values).returning(WEBHOOK_ENDPOINTS)))
            .mappings()
            .one()
        )
        return _webhook_endpoint_row(stored, self.space.slug)

    async def get_webhook_endpoint(self, endpoint_id: UUID) -> WebhookEndpointRow | None:
        row = (
            (
                await self.execute(
                    select(WEBHOOK_ENDPOINTS).where(
                        WEBHOOK_ENDPOINTS.c.id == endpoint_id,
                        WEBHOOK_ENDPOINTS.c.space_id == self.space.id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return _webhook_endpoint_row(row, self.space.slug) if row else None

    async def update_webhook_secret(self, endpoint_id: UUID, secret: EncryptedPayload) -> bool:
        result = await self.execute(
            update(WEBHOOK_ENDPOINTS)
            .where(
                WEBHOOK_ENDPOINTS.c.id == endpoint_id,
                WEBHOOK_ENDPOINTS.c.space_id == self.space.id,
            )
            .values(**secret.columns())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def update_webhook_endpoint(
        self,
        endpoint_id: UUID,
        *,
        url: str,
        description: str | None,
        event_types: list[str],
        filter: dict[str, Any],
        min_severity: str,
    ) -> bool:
        result = await self.execute(
            update(WEBHOOK_ENDPOINTS)
            .where(
                WEBHOOK_ENDPOINTS.c.id == endpoint_id,
                WEBHOOK_ENDPOINTS.c.space_id == self.space.id,
            )
            .values(
                url=url,
                description=description,
                event_types=event_types,
                filter=filter,
                min_severity=min_severity,
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def delete_webhook_endpoint(self, endpoint_id: UUID) -> bool:
        result = await self.execute(
            delete(WEBHOOK_ENDPOINTS).where(
                WEBHOOK_ENDPOINTS.c.id == endpoint_id,
                WEBHOOK_ENDPOINTS.c.space_id == self.space.id,
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def set_webhook_enabled(self, endpoint_id: UUID, enabled: bool, reason: str | None) -> bool:
        values: dict[str, Any] = {
            "enabled": enabled,
            "disabled_reason": None if enabled else reason,
        }
        if enabled:
            values["failure_streak"] = 0
        result = await self.execute(
            update(WEBHOOK_ENDPOINTS)
            .where(
                WEBHOOK_ENDPOINTS.c.id == endpoint_id,
                WEBHOOK_ENDPOINTS.c.space_id == self.space.id,
            )
            .values(**values)
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            return False
        await self.execute(_resume_deliveries(endpoint_id) if enabled else _pause_deliveries(endpoint_id))
        return True

    async def retry_webhook_delivery(self, delivery_id: int) -> bool:
        row = (
            await self.execute(
                select(WEBHOOK_DELIVERIES.c.endpoint_id)
                .join(
                    WEBHOOK_ENDPOINTS,
                    WEBHOOK_ENDPOINTS.c.id == WEBHOOK_DELIVERIES.c.endpoint_id,
                )
                .where(
                    WEBHOOK_DELIVERIES.c.id == delivery_id,
                    WEBHOOK_ENDPOINTS.c.space_id == self.space.id,
                )
                .with_for_update(of=WEBHOOK_DELIVERIES)
            )
        ).one_or_none()
        if row is None:
            return False
        await self.execute(
            update(WEBHOOK_DELIVERIES)
            .where(WEBHOOK_DELIVERIES.c.id == delivery_id)
            .values(
                status=DeliveryStatus.PENDING,
                attempt=0,
                next_attempt_at=func.now(),
                response_code=None,
                response_ms=None,
                error=None,
                delivered_at=None,
            )
        )
        await self.execute(
            update(WEBHOOK_ENDPOINTS)
            .where(WEBHOOK_ENDPOINTS.c.id == row.endpoint_id)
            .values(enabled=True, disabled_reason=None, failure_streak=0)
        )
        await self.execute(_resume_deliveries(row.endpoint_id))
        return True

    async def webhook_deliveries(self, endpoint_id: UUID, limit: int) -> list[WebhookDeliveryRow]:
        rows = (
            await self.execute(
                select(WEBHOOK_DELIVERIES)
                .join(
                    WEBHOOK_ENDPOINTS,
                    WEBHOOK_ENDPOINTS.c.id == WEBHOOK_DELIVERIES.c.endpoint_id,
                )
                .where(
                    WEBHOOK_DELIVERIES.c.endpoint_id == endpoint_id,
                    WEBHOOK_ENDPOINTS.c.space_id == self.space.id,
                )
                .order_by(WEBHOOK_DELIVERIES.c.created_at.desc(), WEBHOOK_DELIVERIES.c.id.desc())
                .limit(limit)
            )
        ).mappings()
        return [_webhook_delivery_row(row) for row in rows]

    async def webhook_endpoint_reads(self) -> list[WebhookEndpointReadRow]:
        e = WEBHOOK_ENDPOINTS.c
        rows = (
            await self.execute(
                select(
                    e.id,
                    e.url,
                    e.description,
                    e.event_types,
                    e.filter,
                    e.min_severity,
                    e.enabled,
                    e.disabled_reason,
                    e.failure_streak,
                    e.created_at,
                )
                .where(e.space_id == self.space.id)
                .order_by(e.created_at.desc(), e.id.desc())
            )
        ).mappings()
        return [WebhookEndpointReadRow(**dict(row)) for row in rows]

    async def webhook_delivery_read_page(
        self, *, limit: int, before: tuple[datetime, int] | None = None
    ) -> list[WebhookDeliveryRow]:
        d, e = WEBHOOK_DELIVERIES.c, WEBHOOK_ENDPOINTS.c
        query = (
            select(WEBHOOK_DELIVERIES)
            .join(WEBHOOK_ENDPOINTS, e.id == d.endpoint_id)
            .where(e.space_id == self.space.id)
        )
        if before is not None:
            query = query.where(tuple_(d.created_at, d.id) < before)
        rows = (await self.execute(query.order_by(d.created_at.desc(), d.id.desc()).limit(limit))).mappings()
        return [_webhook_delivery_row(row) for row in rows]

    # --------------------------------------------------------- Telegram-уведомления

    async def insert_notification_channel(self, row: NewNotificationChannel) -> NotificationChannelRow:
        stored = (
            (
                await self.execute(
                    insert(NOTIFICATION_CHANNELS)
                    .values(
                        id=row.id,
                        space_id=self.space.id,
                        kind="telegram",
                        title=row.title,
                        config=row.config,
                        **row.secret.columns(),
                    )
                    .returning(NOTIFICATION_CHANNELS)
                )
            )
            .mappings()
            .one()
        )
        return _notification_channel_row(stored)

    async def notification_channels(self) -> list[NotificationChannelRow]:
        rows = (
            await self.execute(
                select(NOTIFICATION_CHANNELS)
                .where(NOTIFICATION_CHANNELS.c.space_id == self.space.id)
                .order_by(NOTIFICATION_CHANNELS.c.created_at, NOTIFICATION_CHANNELS.c.id)
            )
        ).mappings()
        return [_notification_channel_row(row) for row in rows]

    async def get_notification_channel_secret(self, channel_id: UUID) -> NotificationChannelSecretRow | None:
        row = (
            (
                await self.execute(
                    select(NOTIFICATION_CHANNELS).where(
                        NOTIFICATION_CHANNELS.c.id == channel_id,
                        NOTIFICATION_CHANNELS.c.space_id == self.space.id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return NotificationChannelSecretRow(
            id=row["id"],
            config=dict(row["config"]),
            secret=EncryptedPayload(row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"]),
        )

    async def update_notification_channel(
        self, channel_id: UUID, values: dict[str, Any], secret: EncryptedPayload | None
    ) -> bool:
        changes = dict(values)
        if secret is not None:
            changes.update(secret.columns())
        if not changes:
            return await self._notification_channel_exists(channel_id)
        result = await self.execute(
            update(NOTIFICATION_CHANNELS)
            .where(
                NOTIFICATION_CHANNELS.c.id == channel_id,
                NOTIFICATION_CHANNELS.c.space_id == self.space.id,
            )
            .values(**changes)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def _notification_channel_exists(self, channel_id: UUID) -> bool:
        return (
            await self.execute(
                select(NOTIFICATION_CHANNELS.c.id).where(
                    NOTIFICATION_CHANNELS.c.id == channel_id,
                    NOTIFICATION_CHANNELS.c.space_id == self.space.id,
                )
            )
        ).scalar_one_or_none() is not None

    async def delete_notification_channel(self, channel_id: UUID) -> bool:
        result = await self.execute(
            delete(NOTIFICATION_CHANNELS).where(
                NOTIFICATION_CHANNELS.c.id == channel_id,
                NOTIFICATION_CHANNELS.c.space_id == self.space.id,
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def insert_notification_rule(self, row: NewNotificationRule) -> NotificationRuleRow:
        if not await self._notification_channel_exists(row.channel_id):
            from identity_hub.errors import NotFound

            raise NotFound("notification channel", row.channel_id)
        stored = (
            (
                await self.execute(
                    insert(NOTIFICATION_RULES)
                    .values(
                        id=row.id,
                        space_id=self.space.id,
                        channel_id=row.channel_id,
                        name=row.name,
                        event_types=row.event_types,
                        min_severity=row.min_severity,
                        filter=row.filter,
                        digest_seconds=row.digest_seconds,
                        template=row.template,
                        created_by=row.created_by,
                    )
                    .returning(NOTIFICATION_RULES)
                )
            )
            .mappings()
            .one()
        )
        return _notification_rule_row(stored)

    async def notification_rules(self) -> list[NotificationRuleRow]:
        rows = (
            await self.execute(
                select(NOTIFICATION_RULES)
                .where(NOTIFICATION_RULES.c.space_id == self.space.id)
                # по имени: порядок не меняется от правки правила и совпадает с MemoryStore
                .order_by(NOTIFICATION_RULES.c.name, NOTIFICATION_RULES.c.id)
            )
        ).mappings()
        return [_notification_rule_row(row) for row in rows]

    async def notification_delivery_read_page(
        self, *, limit: int, before: tuple[datetime, int] | None = None
    ) -> list[NotificationDeliveryReadRow]:
        n, r, c = NOTIFICATIONS.c, NOTIFICATION_RULES.c, NOTIFICATION_CHANNELS.c
        query = (
            select(
                n.id,
                n.rule_id,
                r.name.label("rule_name"),
                n.channel_id,
                c.title.label("channel_title"),
                n.event_ids,
                n.status,
                n.attempt,
                n.scheduled_at,
                n.sent_at,
                n.error,
            )
            .join(NOTIFICATION_RULES, r.id == n.rule_id)
            .join(NOTIFICATION_CHANNELS, c.id == n.channel_id)
            .where(r.space_id == self.space.id, c.space_id == self.space.id)
        )
        if before is not None:
            query = query.where(tuple_(n.scheduled_at, n.id) < before)
        rows = (
            await self.execute(query.order_by(n.scheduled_at.desc(), n.id.desc()).limit(limit))
        ).mappings()
        return [NotificationDeliveryReadRow(**dict(row)) for row in rows]

    async def retry_notification_delivery(self, delivery_id: int) -> bool:
        n, r = NOTIFICATIONS.c, NOTIFICATION_RULES.c
        owned = (
            await self.execute(
                select(n.id)
                .join(NOTIFICATION_RULES, r.id == n.rule_id)
                .where(n.id == delivery_id, r.space_id == self.space.id)
                .with_for_update(of=NOTIFICATIONS)
            )
        ).scalar_one_or_none()
        if owned is None:
            return False
        await self.execute(
            update(NOTIFICATIONS)
            .where(n.id == delivery_id)
            .values(
                status=DeliveryStatus.PENDING,
                attempt=0,
                scheduled_at=func.now(),
                sent_at=None,
                error=None,
            )
        )
        return True

    async def update_notification_rule(self, rule_id: UUID, values: dict[str, Any]) -> bool:
        changes = {**values, "updated_at": func.now()}
        result = await self.execute(
            update(NOTIFICATION_RULES)
            .where(
                NOTIFICATION_RULES.c.id == rule_id,
                NOTIFICATION_RULES.c.space_id == self.space.id,
            )
            .values(**changes)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def delete_notification_rule(self, rule_id: UUID) -> bool:
        result = await self.execute(
            delete(NOTIFICATION_RULES).where(
                NOTIFICATION_RULES.c.id == rule_id,
                NOTIFICATION_RULES.c.space_id == self.space.id,
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    # --------------------------------------------------------- обслуживание

    # --------------------------------------------------------- admin durable state (A4.1)

    @staticmethod
    def _import_run_row(row: Any) -> ImportRunRow:
        values = row._mapping if hasattr(row, "_mapping") else row
        return ImportRunRow(**{name: values[name] for name in IMPORT_RUN_FIELDS})

    async def get_import_run(self, run_id: UUID) -> ImportRunRow | None:
        row = (
            await self.execute(
                select(*(IMPORT_RUNS.c[name] for name in IMPORT_RUN_FIELDS)).where(
                    IMPORT_RUNS.c.id == run_id,
                    IMPORT_RUNS.c.space_id == self.space.id,
                )
            )
        ).one_or_none()
        return self._import_run_row(row) if row is not None else None

    async def list_import_runs(self, *, limit: int = 100) -> list[ImportRunRow]:
        rows = (
            await self.execute(
                select(*(IMPORT_RUNS.c[name] for name in IMPORT_RUN_FIELDS))
                .where(IMPORT_RUNS.c.space_id == self.space.id)
                .order_by(IMPORT_RUNS.c.started_at.desc(), IMPORT_RUNS.c.id.desc())
                .limit(limit)
            )
        ).all()
        return [self._import_run_row(row) for row in rows]

    async def create_import_run(self, run: NewImportRun, items: Sequence[NewImportItem]) -> bool:
        inserted = await self.execute(
            insert(IMPORT_RUNS)
            .values(
                id=run.id,
                space_id=self.space.id,
                service=run.service,
                actor=run.actor,
                source=run.source,
                on_existing=run.on_existing,
                verify_sessions=run.verify_sessions,
                labels=run.labels,
                total=run.total,
            )
            .on_conflict_do_nothing(index_elements=[IMPORT_RUNS.c.id])
        )
        if not inserted.rowcount:  # type: ignore[attr-defined]
            return False
        if items:
            await self.execute(
                insert(IMPORT_ITEMS).values(
                    [
                        {
                            "run_id": run.id,
                            "position": item.position,
                            "space_id": self.space.id,
                            "key_id": item.payload.key_id,
                            "wrapped_dek": item.payload.wrapped_dek,
                            "nonce": item.payload.nonce,
                            "ciphertext": item.payload.ciphertext,
                        }
                        for item in items
                    ]
                )
            )
        return True

    async def active_import_runs(self, *, limit: int = 10) -> list[ImportRunRow]:
        rows = (
            await self.execute(
                select(*(IMPORT_RUNS.c[name] for name in IMPORT_RUN_FIELDS))
                .where(
                    IMPORT_RUNS.c.space_id == self.space.id,
                    IMPORT_RUNS.c.state.in_(("pending", "running", "cancelling")),
                )
                .order_by(IMPORT_RUNS.c.started_at, IMPORT_RUNS.c.id)
                .limit(limit)
            )
        ).all()
        return [self._import_run_row(row) for row in rows]

    async def claim_import_items(
        self,
        run_id: UUID,
        *,
        worker: str,
        limit: int,
        claim_for: timedelta,
    ) -> list[ImportItemRow]:
        run = (
            await self.execute(
                select(IMPORT_RUNS.c.state, IMPORT_RUNS.c.cancel_requested_at)
                .where(
                    IMPORT_RUNS.c.id == run_id,
                    IMPORT_RUNS.c.space_id == self.space.id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if run is None or run.cancel_requested_at is not None or run.state not in {"pending", "running"}:
            return []
        await self.execute(
            update(IMPORT_ITEMS)
            .where(
                IMPORT_ITEMS.c.run_id == run_id,
                IMPORT_ITEMS.c.space_id == self.space.id,
                IMPORT_ITEMS.c.state == "running",
                IMPORT_ITEMS.c.claim_expires_at <= func.now(),
            )
            .values(state="pending", claimed_by=None, claim_expires_at=None)
        )
        candidates = (
            (
                await self.execute(
                    select(IMPORT_ITEMS.c.position)
                    .where(
                        IMPORT_ITEMS.c.run_id == run_id,
                        IMPORT_ITEMS.c.space_id == self.space.id,
                        IMPORT_ITEMS.c.state == "pending",
                    )
                    .order_by(IMPORT_ITEMS.c.position)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not candidates:
            return []
        rows = (
            await self.execute(
                update(IMPORT_ITEMS)
                .where(
                    IMPORT_ITEMS.c.run_id == run_id,
                    IMPORT_ITEMS.c.space_id == self.space.id,
                    IMPORT_ITEMS.c.position.in_(candidates),
                    IMPORT_ITEMS.c.state == "pending",
                )
                .values(
                    state="running",
                    claim_token=IMPORT_ITEMS.c.claim_token + 1,
                    claimed_by=worker,
                    claim_expires_at=func.now() + claim_for,
                )
                .returning(
                    IMPORT_ITEMS.c.run_id,
                    IMPORT_ITEMS.c.position,
                    IMPORT_ITEMS.c.claim_token,
                    IMPORT_ITEMS.c.key_id,
                    IMPORT_ITEMS.c.wrapped_dek,
                    IMPORT_ITEMS.c.nonce,
                    IMPORT_ITEMS.c.ciphertext,
                )
            )
        ).all()
        await self.execute(
            update(IMPORT_RUNS)
            .where(IMPORT_RUNS.c.id == run_id)
            .values(state="running", updated_at=func.now())
        )
        return [
            ImportItemRow(
                run_id=row.run_id,
                position=row.position,
                claim_token=row.claim_token,
                payload=EncryptedPayload(row.key_id, row.wrapped_dek, row.nonce, row.ciphertext),
            )
            for row in sorted(rows, key=lambda item: item.position)
        ]

    async def finish_import_items(
        self, run_id: UUID, results: Sequence[ImportItemResult]
    ) -> ImportRunRow | None:
        run = (
            await self.execute(
                select(*(IMPORT_RUNS.c[name] for name in IMPORT_RUN_FIELDS))
                .where(
                    IMPORT_RUNS.c.id == run_id,
                    IMPORT_RUNS.c.space_id == self.space.id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if run is None:
            return None
        counts = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}
        report = dict(run.report)
        report.setdefault("created_ids", [])
        report.setdefault("skipped", [])
        report.setdefault("failed", [])
        accepted = 0
        for item in results:
            item_state = "succeeded" if item.outcome in {"created", "updated"} else item.outcome
            updated_item = await self.execute(
                update(IMPORT_ITEMS)
                .where(
                    IMPORT_ITEMS.c.run_id == run_id,
                    IMPORT_ITEMS.c.position == item.position,
                    IMPORT_ITEMS.c.space_id == self.space.id,
                    IMPORT_ITEMS.c.state == "running",
                    IMPORT_ITEMS.c.claim_token == item.claim_token,
                )
                .values(
                    state=item_state,
                    claimed_by=None,
                    claim_expires_at=None,
                    error_code=item.error_code,
                    error=item.error[:500] if item.error else None,
                )
            )
            if not updated_item.rowcount:  # type: ignore[attr-defined]
                continue
            accepted += 1
            counts[item.outcome] += 1
            if item.outcome == "created" and item.identity_id is not None:
                report["created_ids"] = [*report["created_ids"][-4999:], str(item.identity_id)]
            elif item.outcome in {"skipped", "failed"}:
                entry = {
                    "position": item.position,
                    "external_ref": item.external_ref,
                    "code": item.error_code,
                    "reason": item.error,
                }
                report[item.outcome] = [*report[item.outcome][-999:], entry]
        remaining_running = (
            await self.execute(
                select(func.count())
                .select_from(IMPORT_ITEMS)
                .where(
                    IMPORT_ITEMS.c.run_id == run_id,
                    IMPORT_ITEMS.c.space_id == self.space.id,
                    IMPORT_ITEMS.c.state == "running",
                )
            )
        ).scalar_one()
        remaining_open = (
            await self.execute(
                select(func.count())
                .select_from(IMPORT_ITEMS)
                .where(
                    IMPORT_ITEMS.c.run_id == run_id,
                    IMPORT_ITEMS.c.space_id == self.space.id,
                    IMPORT_ITEMS.c.state.in_(("pending", "running")),
                )
            )
        ).scalar_one()
        terminal: str | None = None
        if run.cancel_requested_at is not None and remaining_running == 0:
            terminal = "cancelled"
        elif remaining_open == 0:
            unreadable = (
                await self.execute(
                    select(func.count())
                    .select_from(IMPORT_ITEMS)
                    .where(
                        IMPORT_ITEMS.c.run_id == run_id,
                        IMPORT_ITEMS.c.error_code == UNREADABLE_IMPORT_ITEM,
                    )
                )
            ).scalar_one()
            # нерасшифрованные записи — не «успех»: прогон failed, сами записи остаются (R2-S09)
            terminal = "failed" if unreadable else "succeeded"
        values: dict[str, Any] = {
            "processed": IMPORT_RUNS.c.processed + accepted,
            "created": IMPORT_RUNS.c.created + counts["created"],
            "updated": IMPORT_RUNS.c.updated + counts["updated"],
            "skipped": IMPORT_RUNS.c.skipped + counts["skipped"],
            "failed": IMPORT_RUNS.c.failed + counts["failed"],
            "report": report,
            "updated_at": func.now(),
        }
        if terminal is not None:
            values.update(state=terminal, finished_at=func.now())
        await self.execute(update(IMPORT_RUNS).where(IMPORT_RUNS.c.id == run_id).values(**values))
        if terminal is not None:
            finished = delete(IMPORT_ITEMS).where(IMPORT_ITEMS.c.run_id == run_id)
            if terminal == "failed":
                finished = finished.where(IMPORT_ITEMS.c.error_code.is_distinct_from(UNREADABLE_IMPORT_ITEM))
            await self.execute(finished)
        return await self.get_import_run(run_id)

    async def request_import_cancel(self, run_id: UUID) -> ImportRunRow | None:
        run = (
            await self.execute(
                select(IMPORT_RUNS.c.state)
                .where(
                    IMPORT_RUNS.c.id == run_id,
                    IMPORT_RUNS.c.space_id == self.space.id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if run is None:
            return None
        if run.state in {"succeeded", "failed", "cancelled"}:
            return await self.get_import_run(run_id)
        await self.execute(
            update(IMPORT_ITEMS)
            .where(
                IMPORT_ITEMS.c.run_id == run_id,
                IMPORT_ITEMS.c.state == "running",
                IMPORT_ITEMS.c.claim_expires_at <= func.now(),
            )
            .values(state="pending", claimed_by=None, claim_expires_at=None)
        )
        running = (
            await self.execute(
                select(func.count())
                .select_from(IMPORT_ITEMS)
                .where(IMPORT_ITEMS.c.run_id == run_id, IMPORT_ITEMS.c.state == "running")
            )
        ).scalar_one()
        state = "cancelling" if running else "cancelled"
        await self.execute(
            update(IMPORT_RUNS)
            .where(IMPORT_RUNS.c.id == run_id)
            .values(
                state=state,
                cancel_requested_at=func.now(),
                finished_at=func.now() if state == "cancelled" else None,
                updated_at=func.now(),
            )
        )
        if state == "cancelled":
            await self.execute(delete(IMPORT_ITEMS).where(IMPORT_ITEMS.c.run_id == run_id))
        return await self.get_import_run(run_id)

    async def list_provider_operations(
        self,
        *,
        identity_id: UUID | None = None,
        states: frozenset[str] | None = None,
        limit: int = 100,
    ) -> list[ProviderOperationRow]:
        stmt = select(*(PROVIDER_OPERATIONS.c[name] for name in PROVIDER_OPERATION_FIELDS)).where(
            PROVIDER_OPERATIONS.c.space_id == self.space.id
        )
        if identity_id is not None:
            stmt = stmt.where(PROVIDER_OPERATIONS.c.identity_id == identity_id)
        if states is not None:
            if not states:
                return []
            stmt = stmt.where(PROVIDER_OPERATIONS.c.state.in_(sorted(states)))
        rows = (
            await self.execute(
                stmt.order_by(
                    PROVIDER_OPERATIONS.c.created_at.desc(),
                    PROVIDER_OPERATIONS.c.id.desc(),
                ).limit(limit)
            )
        ).all()
        return [
            ProviderOperationRow(
                id=row.id,
                space_id=row.space_id,
                identity_id=row.identity_id,
                kind=row.kind,
                state=row.state,
                attempt=row.attempt,
                claim_id=row.claim_id,
                claim_token=row.claim_token,
                claimed_by=row.claimed_by,
                claim_expires_at=row.claim_expires_at,
                available_at=row.available_at,
                deadline_at=row.deadline_at,
                result=dict(row.result),
                error_code=row.error_code,
                created_at=row.created_at,
                updated_at=row.updated_at,
                finished_at=row.finished_at,
            )
            for row in rows
        ]

    async def due_identity_timers(self, limit: int) -> list[tuple[UUID, str]]:
        i = IDENTITIES.c
        # Только состояния с переходом по таймеру: строка `frozen` с истёкшим `until` иначе вечно
        # стояла бы первой в выборке и заслоняла остальные (R2-F06).
        availability_due = and_(
            i.availability.in_(sorted(AVAILABILITY.on_expire)), i.availability_until <= func.now()
        )
        pinned = (
            select(REGION_STATES.c.identity_id)
            .where(
                REGION_STATES.c.identity_id == i.id,
                REGION_STATES.c.region == "standing",
                REGION_STATES.c.key == "",
                REGION_STATES.c.pinned_until > func.now(),
            )
            .exists()
        )
        # закреплённый standing таймер не трогает: строка не должна занимать очередь (R2-F14)
        standing_due = and_(
            i.standing.in_(sorted(STANDING.on_expire)), i.standing_until <= func.now(), ~pinned
        )
        first_due = func.least(
            case((availability_due, i.availability_until)), case((standing_due, i.standing_until))
        )
        rows = (
            await self.execute(
                select(
                    i.id,
                    availability_due.label("availability_due"),
                    standing_due.label("standing_due"),
                )
                .where(i.space_id == self.space.id, or_(availability_due, standing_due))
                .order_by(first_due.asc().nulls_last(), i.id)
                .limit(limit)
            )
        ).mappings()
        result: list[tuple[UUID, str]] = []
        for row in rows:
            if row["availability_due"]:
                result.append((row["id"], "availability"))
            if row["standing_due"]:
                result.append((row["id"], "standing"))
        return result

    async def stuck_logins(self, limit: int) -> list[tuple[UUID, datetime]]:
        i, r = IDENTITIES.c, REGION_STATES.c
        since = func.coalesce(r.changed_at, i.updated_at).label("since")
        live = (
            select(LEASES.c.identity_id)
            .where(LEASES.c.identity_id == i.id, LEASES.c.expires_at > func.now())
            .exists()
        )
        rows = await self.execute(
            select(i.id, since)
            .select_from(
                IDENTITIES.outerjoin(
                    REGION_STATES,
                    and_(r.identity_id == i.id, r.region == "auth", r.key == "default"),
                )
            )
            .where(i.space_id == self.space.id, i.auth_state == AuthState.LOGGING_IN, ~live)
            .order_by(since, i.id)
            .limit(limit)
        )
        return [(row.id, row.since) for row in rows]

    async def expire_leases(self, limit: int) -> int:
        # Отбор идёт от открытой истории (частичный индекс ix_ih_lease_history_open_space):
        # release оставляет строку ih_leases с expires_at = now(), и отбор по одному expires_at
        # отдавал бы пачку давно закрытых аренд вместо брошенной (R2-D01).
        rows = list(
            (
                await self.execute(
                    select(LEASES, IDENTITIES.c.service)
                    .join(HISTORY, HISTORY.c.lease_id == LEASES.c.lease_id)
                    .join(IDENTITIES, IDENTITIES.c.id == LEASES.c.identity_id)
                    .where(
                        HISTORY.c.space_id == self.space.id,
                        HISTORY.c.released_at.is_(None),
                        LEASES.c.space_id == self.space.id,
                        LEASES.c.expires_at <= func.now(),
                    )
                    .order_by(LEASES.c.expires_at)
                    .limit(limit)
                    .with_for_update(of=LEASES, skip_locked=True)
                )
            ).mappings()
        )
        drafts: list[EventDraft] = []
        for row in rows:
            closed = await self.execute(
                update(HISTORY)
                .where(HISTORY.c.lease_id == row["lease_id"], HISTORY.c.released_at.is_(None))
                .values(released_at=row["expires_at"], end_reason=LeaseEndReason.EXPIRED)
            )
            if not closed.rowcount:  # type: ignore[attr-defined]
                continue
            await self.execute(
                update(LEASES).where(LEASES.c.identity_id == row["identity_id"]).values(idempotency_key=None)
            )
            drafts.append(
                EventDraft(
                    "lease.expired",
                    actor="maintenance",
                    service=row["service"],
                    identity_id=row["identity_id"],
                    lease_id=row["lease_id"],
                    data={
                        "pool": row["pool"],
                        "holder": row["holder"],
                        "fencing": row["fencing"],
                        "expired_at": row["expires_at"].isoformat(),
                    },
                )
            )
        await self.emit(drafts)
        return len(drafts)


_CREDENTIAL_FIELDS = tuple(f.name for f in fields(CredentialRow))
_CREDENTIAL_COLUMNS = tuple(CREDENTIALS.c[name] for name in _CREDENTIAL_FIELDS)


def _credential_row(row: Any) -> CredentialRow:
    return CredentialRow(**{name: row[name] for name in _CREDENTIAL_FIELDS})


def _event(row: Any) -> Event:
    return Event(
        id=row["id"],
        event_id=row["event_id"],
        type=row["type"],
        severity=Severity(row["severity"]),
        occurred_at=row["occurred_at"],
        space=row["slug"],
        service=row["service"],
        identity_id=row["identity_id"],
        proxy_id=row["proxy_id"],
        lease_id=row["lease_id"],
        sequence=row["sequence"],
        actor=row["actor"],
        data=row["data"],
        revision=row["revision"],
    )


def _webhook_endpoint_row(row: Any, space_slug: str) -> WebhookEndpointRow:
    return WebhookEndpointRow(
        id=row["id"],
        space_id=row["space_id"],
        space_slug=space_slug,
        url=row["url"],
        description=row["description"],
        event_types=list(row["event_types"]),
        filter=dict(row["filter"]),
        min_severity=row["min_severity"],
        enabled=row["enabled"],
        disabled_reason=row["disabled_reason"],
        failure_streak=row["failure_streak"],
        created_at=row["created_at"],
        secret=EncryptedPayload(row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"]),
    )


def _webhook_delivery_row(row: Any) -> WebhookDeliveryRow:
    return WebhookDeliveryRow(
        id=row["id"],
        endpoint_id=row["endpoint_id"],
        event_id=row["event_id"],
        status=row["status"],
        attempt=row["attempt"],
        next_attempt_at=row["next_attempt_at"],
        response_code=row["response_code"],
        response_ms=row["response_ms"],
        error=row["error"],
        created_at=row["created_at"],
        delivered_at=row["delivered_at"],
    )


def _notification_channel_row(row: Any) -> NotificationChannelRow:
    return NotificationChannelRow(
        id=row["id"],
        title=row["title"],
        config=dict(row["config"]),
        enabled=row["enabled"],
        created_at=row["created_at"],
    )


def _notification_rule_row(row: Any) -> NotificationRuleRow:
    return NotificationRuleRow(
        id=row["id"],
        channel_id=row["channel_id"],
        name=row["name"],
        enabled=row["enabled"],
        event_types=list(row["event_types"]),
        min_severity=row["min_severity"],
        filter=dict(row["filter"]),
        digest_seconds=row["digest_seconds"],
        template=row["template"],
        created_by=row["created_by"],
        updated_at=row["updated_at"],
    )


def _challenge_row(row: Any) -> ChallengeRow:
    return ChallengeRow(
        id=row["id"],
        identity_id=row["identity_id"],
        kind=row["kind"],
        hint=row["hint"],
        context=EncryptedPayload(
            row["context_key_id"], row["context_wrapped_dek"], row["context_nonce"], row["context_ciphertext"]
        ),
        answer_nonce=row["answer_nonce"],
        answer_ciphertext=row["answer_ciphertext"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        answered_at=row["answered_at"],
        resolved_at=row["resolved_at"],
    )


def _proxy_row(row: Any) -> ProxyRow:
    values = {f: row[f] for f in PROXY_FIELDS if f != "secret"}
    secret = EncryptedPayload(row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"])
    return ProxyRow(**values, secret=secret)


def _counter_values(counters: LeaseCounters) -> dict[str, Any]:
    if counters.is_empty:
        return {}
    values: dict[str, Any] = {
        "succeeded": HISTORY.c.succeeded + counters.succeeded,
        "failed": HISTORY.c.failed + counters.failed,
    }
    if counters.operations:
        values["operations"] = _merge_operations(counters.operations)
    if counters.last_error is not None:
        values["last_error"] = counters.last_error
    return values


def _merge_operations(delta: dict[str, int]) -> Any:
    """operations = operations || {k: operations[k] + delta[k]} для каждого вида операции."""
    expr: Any = HISTORY.c.operations
    for kind, n in sorted(delta.items()):
        current = func.coalesce(HISTORY.c.operations[kind].astext.cast(BigInteger), 0)
        expr = expr.op("||")(func.jsonb_build_object(kind, current + n))
    return expr


def _lease_row(row: Any) -> LeaseRow:
    return LeaseRow(
        lease_id=row["lease_id"],
        token=row["token"],
        identity_id=row["identity_id"],
        fencing=row["fencing"],
        pool=row["pool"],
        holder=row["holder"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
        idempotency_key=row["idempotency_key"],
        proxy_id=row.get("proxy_id"),
        heartbeat_at=row.get("heartbeat_at", row["acquired_at"]),
    )


def cast_uuid_array(ids: list[UUID]) -> Any:
    return sa_cast(ids, ARRAY(PGUUID(as_uuid=True)))


_BUSY_SQLSTATES = frozenset(
    {
        "55P03",  # lock_not_available (lock_timeout)
        "57014",  # query_canceled (statement_timeout)
        "40P01",  # deadlock_detected
        "40001",  # serialization_failure
    }
)


def _translate(exc: DBAPIError) -> IdentityHubError:
    """Ошибка драйвера → ошибка библиотеки без SQL и параметров (S-8, R2-S03).

    Текст `DBAPIError` содержит `[parameters: ...]`: среди них `token` аренды и шифртексты.
    """
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    sqlstate = str(sqlstate) if sqlstate else None
    if sqlstate in _BUSY_SQLSTATES:
        return DatabaseBusy(sqlstate=sqlstate)
    return StorageError(sqlstate)


async def _execute(session: AsyncSession, stmt: Any, *args: Any, **kwargs: Any) -> Result[Any]:
    """`session.execute` с переводом ошибки драйвера; исходное исключение не остаётся в цепочке."""
    try:
        result: Result[Any] = await session.execute(stmt, *args, **kwargs)
        return result
    except DBAPIError as exc:
        failure = _translate(exc)
    raise failure  # вне except: у ошибки нет ни __cause__, ни __context__ с параметрами запроса


class PgStore(Store):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], db_schema: str | None = None) -> None:
        self.sessionmaker = sessionmaker
        self.db_schema = db_schema
        self.options: dict[str, Any] = schema_options(db_schema)

    @asynccontextmanager
    async def _own(self) -> AsyncIterator[AsyncSession]:
        try:
            async with self.sessionmaker() as session, session.begin():
                yield session
        except DBAPIError as exc:  # ошибка вне отдельного запроса: connect, commit, savepoint
            raise _translate(exc) from None

    @asynccontextmanager
    async def transaction(self, space: SpaceRef, *, session: Any | None = None) -> AsyncIterator[PgTx]:
        if session is None:
            async with self._own() as own:
                await self._bind(own, space)
                yield PgTx(self, own, space)
            return
        if not isinstance(session, AsyncSession):
            raise TypeError("using() принимает sqlalchemy.ext.asyncio.AsyncSession")
        bound = session.info.get(SESSION_SPACE_KEY)
        if bound is not None and bound != space.id:
            raise SpaceMismatch(f"сессия уже связана с другим пространством, а не {space.slug!r}")
        session.info[SESSION_SPACE_KEY] = space.id
        try:
            async with session.begin_nested():
                await self._bind(session, space)
                yield PgTx(self, session, space)
        except DBAPIError as exc:
            raise _translate(exc) from None

    @staticmethod
    async def _bind(session: AsyncSession, space: SpaceRef) -> None:
        await _execute(session, text("SELECT set_config('ih.space', :slug, true)"), {"slug": space.slug})

    # --------------------------------------------------------- реестр и пространства

    async def current_time(self) -> datetime:
        async with self.sessionmaker() as session:
            value: datetime = (await _execute(session, select(func.now()))).scalar_one()
        return value

    async def schema_revisions(self) -> tuple[str, list[str]]:
        version = Table(VERSION_TABLE, MetaData(), Column("version_num", String(32)))
        async with self.sessionmaker() as session:
            try:
                rows = await session.execute(
                    select(version.c.version_num).order_by(version.c.version_num),
                    execution_options=self.options,
                )
            except ProgrammingError:  # таблицы версий нет — схема не создана
                return SCHEMA_HEAD, []
            revisions = [str(row[0]) for row in rows]
            return SCHEMA_HEAD, revisions

    def compatible_revisions(self, expected: str) -> frozenset[str]:
        return SCHEMA_COMPATIBLE

    async def sync_services(self, services: Sequence[ServiceRow]) -> None:
        if not services:
            return
        stmt = insert(ServiceRecord).values([{"name": s.name, "title": s.title} for s in services])
        stmt = stmt.on_conflict_do_update(
            index_elements=[ServiceRecord.name],
            set_={"title": stmt.excluded.title, "synced_at": func.now()},
        )
        async with self._own() as session:
            await _execute(session, stmt, execution_options=self.options)

    async def get_space(self, slug: str) -> SpaceRef | None:
        async with self.sessionmaker() as session:
            row = (
                await _execute(
                    session,
                    select(SpaceRecord.id, SpaceRecord.slug, SpaceRecord.title, SpaceRecord.timezone).where(
                        SpaceRecord.slug == slug
                    ),
                    execution_options=self.options,
                )
            ).one_or_none()
        return SpaceRef(row.id, row.slug, row.title, row.timezone) if row else None

    async def list_spaces(self, slugs: frozenset[str] | None = None) -> list[SpaceRef]:
        stmt = select(SpaceRecord.id, SpaceRecord.slug, SpaceRecord.title, SpaceRecord.timezone).order_by(
            SpaceRecord.slug
        )
        if slugs is not None:
            if not slugs:
                return []
            stmt = stmt.where(SpaceRecord.slug.in_(sorted(slugs)))
        async with self.sessionmaker() as session:
            rows = await _execute(session, stmt, execution_options=self.options)
        return [SpaceRef(row.id, row.slug, row.title, row.timezone) for row in rows]

    # --------------------------------------------------------- журнал событий

    async def process_events(
        self,
        consumer: str,
        handler: Callable[[list[Event]], Awaitable[int | None]],
        *,
        limit: int,
        external_only: bool = True,
    ) -> int | None:
        opts = self.options
        async with self._own() as session:
            await _execute(
                session,
                insert(CURSORS).values(consumer=consumer, last_id=0).on_conflict_do_nothing(),
                execution_options=opts,
            )
        async with self._own() as session:
            locked = (
                await _execute(
                    session,
                    select(CURSORS.c.consumer)
                    .where(CURSORS.c.consumer == consumer)
                    .with_for_update(skip_locked=True),
                    execution_options=opts,
                )
            ).scalar_one_or_none()
            if locked is None:
                return None  # пачку этого потребителя обрабатывает другой экземпляр (G-5.5)
            cursor = (
                select(CURSORS.c.last_tx_id, CURSORS.c.last_id)
                .where(CURSORS.c.consumer == consumer)
                .subquery()
            )
            horizon = func.pg_snapshot_xmin(func.pg_current_snapshot())
            e = EVENTS.c
            conditions = [e.tx_id < horizon]
            if external_only:
                conditions.append(e.external.is_(True))
            stmt = (
                select(*e, SPACES.c.slug)
                .join(SPACES, SPACES.c.id == e.space_id)
                .join(cursor, tuple_(e.tx_id, e.id) > tuple_(cursor.c.last_tx_id, cursor.c.last_id))
                .where(*conditions)
                .order_by(e.tx_id, e.id)
                .limit(limit)
            )
            rows = list((await _execute(session, stmt, execution_options=opts)).mappings())
            if not rows:
                return 0
            events = [_event(r) for r in rows]
            done = await handler(events)
            handled = len(events) if done is None else max(0, min(done, len(events)))
            if handled == 0:
                return 0
            last_id = rows[handled - 1]["id"]
            await _execute(
                session,
                update(CURSORS)
                .where(CURSORS.c.consumer == consumer)
                .values(
                    last_tx_id=select(e.tx_id).where(e.id == last_id).scalar_subquery(),
                    last_id=last_id,
                    updated_at=func.now(),
                ),
                execution_options=opts,
            )
            return handled

    async def rollup_source_events(self, space_slug: str, start: datetime, end: datetime) -> list[Event]:
        async with self.sessionmaker() as session:
            rows = (
                await _execute(
                    session,
                    select(EVENTS, SPACES.c.slug)
                    .join(SPACES, SPACES.c.id == EVENTS.c.space_id)
                    .where(
                        SPACES.c.slug == space_slug,
                        EVENTS.c.occurred_at >= start,
                        EVENTS.c.occurred_at < end,
                    )
                    .order_by(EVENTS.c.occurred_at, EVENTS.c.id),
                    execution_options=self.options,
                )
            ).mappings()
            return [_event(row) for row in rows]

    async def replace_rollups(self, rows: Sequence[RollupRow]) -> None:
        if not rows:
            return
        values = [
            {
                "space_id": row.key.space_id,
                "service": row.key.service,
                "metric": row.key.metric,
                "dims": row.key.dimensions,
                "bucket_start": row.key.bucket_start,
                "bucket_size": str(row.key.bucket_size),
                "value": row.value,
            }
            for row in rows
        ]
        stmt = insert(ROLLUPS).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_ih_stats_rollup_key",
            set_={"value": stmt.excluded.value, "updated_at": func.now()},
        )
        async with self._own() as session:
            await _execute(session, stmt, execution_options=self.options)

    async def apply_metric_rollup_batch(self, batch_id: UUID, rows: Sequence[RollupDelta]) -> bool:
        async with self._own() as session:
            inserted = (
                await _execute(
                    session,
                    insert(METRIC_BATCHES)
                    .values(id=batch_id)
                    .on_conflict_do_nothing()
                    .returning(METRIC_BATCHES.c.id),
                    execution_options=self.options,
                )
            ).scalar_one_or_none()
            if inserted is None:
                return False
            for delta in rows:
                row = delta.row
                stmt = insert(ROLLUPS).values(
                    space_id=row.key.space_id,
                    service=row.key.service,
                    metric=row.key.metric,
                    dims=row.key.dimensions,
                    bucket_start=row.key.bucket_start,
                    bucket_size=str(row.key.bucket_size),
                    value=row.value,
                )
                value = ROLLUPS.c.value + stmt.excluded.value if delta.additive else stmt.excluded.value
                await _execute(
                    session,
                    stmt.on_conflict_do_update(
                        constraint="uq_ih_stats_rollup_key",
                        set_={"value": value, "updated_at": func.now()},
                    ),
                    execution_options=self.options,
                )
            return True

    async def event_rollup_rows(
        self, space_id: UUID, bucket_size: BucketSize, start: datetime, end: datetime
    ) -> list[RollupRow]:
        async with self.sessionmaker() as session:
            rows = (
                await _execute(
                    session,
                    select(ROLLUPS).where(
                        ROLLUPS.c.space_id == space_id,
                        ROLLUPS.c.bucket_size == str(bucket_size),
                        ROLLUPS.c.bucket_start >= start,
                        ROLLUPS.c.bucket_start < end,
                        ROLLUPS.c.metric.not_like("telemetry.%"),
                    ),
                    execution_options=self.options,
                )
            ).mappings()
            return [_rollup_row(row) for row in rows]

    async def clear_rollups(self, space_id: UUID, start: datetime, end: datetime) -> int:
        async with self._own() as session:
            result: Any = await _execute(
                session,
                delete(ROLLUPS).where(
                    ROLLUPS.c.space_id == space_id,
                    ROLLUPS.c.bucket_start >= start,
                    ROLLUPS.c.bucket_start < end,
                    # телеметрия приходит из буфера метрик, по журналу событий её не восстановить
                    ROLLUPS.c.metric.not_like("telemetry.%"),
                ),
                execution_options=self.options,
            )
            return int(result.rowcount or 0)

    async def purge_minute_rollups(self, before: datetime) -> int:
        async with self._own() as session:
            result: Any = await _execute(
                session,
                delete(ROLLUPS).where(
                    ROLLUPS.c.bucket_size == str(BucketSize.MINUTE),
                    ROLLUPS.c.bucket_start < before,
                ),
                execution_options=self.options,
            )
            return int(result.rowcount or 0)

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
        stmt = (
            select(ROLLUPS)
            .join(SPACES, SPACES.c.id == ROLLUPS.c.space_id)
            .where(
                SPACES.c.slug == space_slug,
                ROLLUPS.c.metric == metric,
                ROLLUPS.c.bucket_size == str(bucket_size),
                ROLLUPS.c.bucket_start >= start,
                ROLLUPS.c.bucket_start < end,
            )
            .order_by(ROLLUPS.c.bucket_start, ROLLUPS.c.service, ROLLUPS.c.dims)
        )
        if service is not None:
            stmt = stmt.where(ROLLUPS.c.service == service)
        if dims is not None:
            stmt = stmt.where(ROLLUPS.c.dims == dict(dims))
        async with self.sessionmaker() as session:
            records = (await _execute(session, stmt, execution_options=self.options)).mappings()
            return [_rollup_row(row) for row in records]

    async def capture_state_snapshot(self, taken_at: datetime, *, source: str) -> int:
        async with self._own() as session:
            space_ids: list[UUID] = list(
                (
                    await _execute(
                        session,
                        select(SPACES.c.id).order_by(SPACES.c.id),
                        execution_options=self.options,
                    )
                ).scalars()
            )
            if not space_ids:
                return 0
            await _execute(
                session,
                insert(SNAPSHOT_COVERAGE)
                .values(
                    [
                        {
                            "space_id": space_id,
                            "taken_at": taken_at,
                            "source": source,
                            "complete": True,
                        }
                        for space_id in space_ids
                    ]
                )
                .on_conflict_do_nothing(),
                execution_options=self.options,
            )
            await _execute(
                session,
                select(SNAPSHOT_COVERAGE.c.space_id)
                .where(SNAPSHOT_COVERAGE.c.taken_at == taken_at)
                .order_by(SNAPSHOT_COVERAGE.c.space_id)
                .with_for_update(),
                execution_options=self.options,
            )
            await _execute(
                session,
                update(SNAPSHOT_COVERAGE)
                .where(SNAPSHOT_COVERAGE.c.taken_at == taken_at)
                .values(captured_at=func.now(), source=source, complete=True),
                execution_options=self.options,
            )
            await _execute(
                session,
                delete(STATE_SNAPSHOTS).where(STATE_SNAPSHOTS.c.taken_at == taken_at),
                execution_options=self.options,
            )
            region_name = case(
                (
                    REGION_STATES.c.key != "",
                    func.concat(REGION_STATES.c.region, literal(":"), REGION_STATES.c.key),
                ),
                else_=REGION_STATES.c.region,
            )
            grouped = list(
                (
                    await _execute(
                        session,
                        select(
                            IDENTITIES.c.space_id,
                            IDENTITIES.c.service,
                            region_name.label("region"),
                            REGION_STATES.c.state,
                            func.count().label("count"),
                        )
                        .join(REGION_STATES, REGION_STATES.c.identity_id == IDENTITIES.c.id)
                        .group_by(
                            IDENTITIES.c.space_id,
                            IDENTITIES.c.service,
                            region_name,
                            REGION_STATES.c.state,
                        ),
                        execution_options=self.options,
                    )
                ).mappings()
            )
            if grouped:
                await _execute(
                    session,
                    insert(STATE_SNAPSHOTS).values(
                        [
                            {
                                "space_id": row["space_id"],
                                "service": row["service"],
                                "region": row["region"],
                                "state": row["state"],
                                "taken_at": taken_at,
                                "count": row["count"],
                            }
                            for row in grouped
                        ]
                    ),
                    execution_options=self.options,
                )
            return len(grouped)

    async def read_state_snapshots(
        self, space_slug: str, start: datetime, end: datetime
    ) -> list[StateSnapshotFrame]:
        async with self.sessionmaker() as session:
            coverage = list(
                (
                    await _execute(
                        session,
                        select(SNAPSHOT_COVERAGE)
                        .join(SPACES, SPACES.c.id == SNAPSHOT_COVERAGE.c.space_id)
                        .where(
                            SPACES.c.slug == space_slug,
                            SNAPSHOT_COVERAGE.c.taken_at >= start,
                            SNAPSHOT_COVERAGE.c.taken_at < end,
                        )
                        .order_by(SNAPSHOT_COVERAGE.c.taken_at),
                        execution_options=self.options,
                    )
                ).mappings()
            )
            if not coverage:
                return []
            space_id = coverage[0]["space_id"]
            rows = list(
                (
                    await _execute(
                        session,
                        select(STATE_SNAPSHOTS)
                        .where(
                            STATE_SNAPSHOTS.c.space_id == space_id,
                            STATE_SNAPSHOTS.c.taken_at >= start,
                            STATE_SNAPSHOTS.c.taken_at < end,
                        )
                        .order_by(
                            STATE_SNAPSHOTS.c.taken_at,
                            STATE_SNAPSHOTS.c.service,
                            STATE_SNAPSHOTS.c.region,
                            STATE_SNAPSHOTS.c.state,
                        ),
                        execution_options=self.options,
                    )
                ).mappings()
            )
        by_hour: defaultdict[datetime, list[StateSnapshotRow]] = defaultdict(list)
        for row in rows:
            by_hour[row["taken_at"]].append(
                StateSnapshotRow(
                    row["space_id"],
                    row["service"],
                    row["region"],
                    row["state"],
                    row["taken_at"],
                    row["count"],
                )
            )
        return [
            StateSnapshotFrame(
                row["taken_at"],
                row["captured_at"],
                row["source"],
                row["complete"],
                tuple(by_hour[row["taken_at"]]),
            )
            for row in coverage
        ]

    async def purge_state_snapshots(self, before: datetime) -> int:
        async with self._own() as session:
            result: Any = await _execute(
                session,
                delete(SNAPSHOT_COVERAGE).where(SNAPSHOT_COVERAGE.c.taken_at < before),
                execution_options=self.options,
            )
            return int(result.rowcount or 0)

    # --------------------------------------------------------- исходящие вебхуки

    async def enabled_webhook_endpoints(self) -> list[WebhookEndpointRow]:
        async with self.sessionmaker() as session:
            rows = (
                await _execute(
                    session,
                    select(WEBHOOK_ENDPOINTS, SPACES.c.slug.label("space_slug"))
                    .join(SPACES, SPACES.c.id == WEBHOOK_ENDPOINTS.c.space_id)
                    .where(WEBHOOK_ENDPOINTS.c.enabled.is_(True)),
                    execution_options=self.options,
                )
            ).mappings()
            return [_webhook_endpoint_row(row, row["space_slug"]) for row in rows]

    async def identity_labels(self, identity_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
        if not identity_ids:
            return {}
        async with self.sessionmaker() as session:
            rows = await _execute(
                session,
                select(IDENTITIES.c.id, IDENTITIES.c.labels).where(
                    _any_of(IDENTITIES.c.id, set(identity_ids))
                ),
                execution_options=self.options,
            )
        return {row.id: dict(row.labels) for row in rows}

    async def enqueue_webhook_deliveries(self, pairs: Sequence[tuple[UUID, UUID]]) -> int:
        unique = set(pairs)
        if not unique:
            return 0
        stmt = (
            insert(WEBHOOK_DELIVERIES)
            .values(
                [
                    {
                        "endpoint_id": endpoint_id,
                        "event_id": event_id,
                        "status": DeliveryStatus.PENDING,
                        "attempt": 0,
                    }
                    for endpoint_id, event_id in unique
                ]
            )
            .on_conflict_do_nothing(constraint="uq_ih_webhook_deliveries_endpoint_event")
        )
        async with self._own() as session:
            result = await _execute(session, stmt, execution_options=self.options)
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def claim_webhook_deliveries(self, *, limit: int, claim_for: timedelta) -> list[WebhookAttempt]:
        d, ep, e = WEBHOOK_DELIVERIES.c, WEBHOOK_ENDPOINTS.c, EVENTS.c
        async with self._own() as session:
            locked: list[int] = list(
                (
                    await _execute(
                        session,
                        select(d.id)
                        .join(WEBHOOK_ENDPOINTS, ep.id == d.endpoint_id)
                        .where(
                            d.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                            d.next_attempt_at <= func.now(),
                            ep.enabled.is_(True),
                            # доставка без события не отправится никогда: её удалит retention
                            select(literal(1)).where(e.event_id == d.event_id).exists(),
                        )
                        .order_by(d.next_attempt_at, d.id)
                        .limit(limit)
                        .with_for_update(of=WEBHOOK_DELIVERIES, skip_locked=True),
                        execution_options=self.options,
                    )
                ).scalars()
            )
            if not locked:
                return []
            await _execute(
                session,
                update(WEBHOOK_DELIVERIES)
                .where(d.id.in_(locked))
                .values(attempt=d.attempt + 1, next_attempt_at=func.now() + claim_for),
                execution_options=self.options,
            )
            rows = (
                await _execute(
                    session,
                    select(
                        EVENTS,
                        SPACES.c.slug,
                        d.id.label("delivery_id"),
                        d.attempt.label("delivery_attempt"),
                        ep.id.label("endpoint_id"),
                        ep.url.label("endpoint_url"),
                        ep.key_id,
                        ep.wrapped_dek,
                        ep.nonce,
                        ep.ciphertext,
                    )
                    .select_from(WEBHOOK_DELIVERIES)
                    .join(WEBHOOK_ENDPOINTS, ep.id == d.endpoint_id)
                    .join(EVENTS, e.event_id == d.event_id)
                    .join(SPACES, SPACES.c.id == e.space_id)
                    .where(d.id.in_(locked))
                    .order_by(d.id),
                    execution_options=self.options,
                )
            ).mappings()
            return [
                WebhookAttempt(
                    delivery_id=row["delivery_id"],
                    attempt=row["delivery_attempt"],
                    endpoint_id=row["endpoint_id"],
                    url=row["endpoint_url"],
                    secret=EncryptedPayload(
                        row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"]
                    ),
                    event=_event(row),
                )
                for row in rows
            ]

    async def release_webhook_deliveries(
        self, claims: Sequence[tuple[int, int]], *, retry_after: timedelta
    ) -> int:
        if not claims:
            return 0
        d = WEBHOOK_DELIVERIES.c
        async with self._own() as session:
            result = await _execute(
                session,
                update(WEBHOOK_DELIVERIES)
                .where(
                    tuple_(d.id, d.attempt).in_([(delivery_id, attempt) for delivery_id, attempt in claims]),
                    d.status.in_(_FINISHABLE_DELIVERY),
                    d.attempt > 0,
                )
                .values(attempt=d.attempt - 1, next_attempt_at=func.now() + retry_after),
                execution_options=self.options,
            )
            return int(result.rowcount)  # type: ignore[attr-defined]

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
        d, ep = WEBHOOK_DELIVERIES.c, WEBHOOK_ENDPOINTS.c
        async with self._own() as session:
            row = (
                await _execute(
                    session,
                    select(
                        d.endpoint_id,
                        d.attempt,
                        d.status,
                        ep.space_id,
                        ep.failure_streak,
                        ep.enabled,
                        SPACES.c.slug,
                    )
                    .join(WEBHOOK_ENDPOINTS, ep.id == d.endpoint_id)
                    .join(SPACES, SPACES.c.id == ep.space_id)
                    .where(d.id == delivery_id)
                    .with_for_update(of=(WEBHOOK_DELIVERIES, WEBHOOK_ENDPOINTS)),
                    execution_options=self.options,
                )
            ).one_or_none()
            if row is None or row.attempt != attempt or row.status not in _FINISHABLE_DELIVERY:
                return False
            common = {
                "response_code": response_code,
                "response_ms": response_ms,
                "error": error[:2000] if error else None,
            }
            if succeeded:
                await _execute(
                    session,
                    update(WEBHOOK_DELIVERIES)
                    .where(d.id == delivery_id)
                    .values(**common, status=DeliveryStatus.SUCCEEDED, delivered_at=func.now()),
                    execution_options=self.options,
                )
                await _execute(
                    session,
                    update(WEBHOOK_ENDPOINTS).where(ep.id == row.endpoint_id).values(failure_streak=0),
                    execution_options=self.options,
                )
                return True

            streak = row.failure_streak + 1
            exhausted = streak >= max_failures
            # endpoint, уже отключённый вручную, не отключается повторно: его причина сохраняется
            disabled = exhausted and row.enabled
            if exhausted:
                status = DeliveryStatus.DEAD
            elif row.status == DeliveryStatus.PAUSED:
                status = DeliveryStatus.PAUSED  # endpoint отключён: повтор — после включения
            else:
                status = DeliveryStatus.FAILED
            await _execute(
                session,
                update(WEBHOOK_DELIVERIES)
                .where(d.id == delivery_id)
                .values(**common, status=status, next_attempt_at=func.now() + retry_after),
                execution_options=self.options,
            )
            endpoint_values: dict[str, Any] = {"failure_streak": streak}
            if disabled:
                endpoint_values.update(enabled=False, disabled_reason="consecutive_failures")
            await _execute(
                session,
                update(WEBHOOK_ENDPOINTS).where(ep.id == row.endpoint_id).values(**endpoint_values),
                execution_options=self.options,
            )
            if disabled:
                await _execute(session, _pause_deliveries(row.endpoint_id), execution_options=self.options)
                await _execute(
                    session,
                    insert(EVENTS).values(
                        event_id=new_id(),
                        type="webhook.endpoint.disabled",
                        severity=Severity.CRITICAL,
                        space_id=row.space_id,
                        actor="system:webhooks",
                        data={"endpoint_id": str(row.endpoint_id), "failure_streak": streak},
                        external=True,
                    ),
                    execution_options=self.options,
                )
                await _execute(
                    session,
                    text("SELECT pg_notify(:channel, :payload)"),
                    {"channel": NOTIFY_CHANNEL, "payload": row.slug},
                )
            return True

    # --------------------------------------------------------- Telegram-уведомления

    async def enabled_notification_targets(self) -> list[NotificationTargetRow]:
        r, c = NOTIFICATION_RULES.c, NOTIFICATION_CHANNELS.c
        async with self.sessionmaker() as session:
            rows = (
                await _execute(
                    session,
                    select(
                        NOTIFICATION_RULES,
                        SPACES.c.slug.label("space_slug"),
                        c.config.label("channel_config"),
                        c.key_id,
                        c.wrapped_dek,
                        c.nonce,
                        c.ciphertext,
                    )
                    .join(NOTIFICATION_CHANNELS, c.id == r.channel_id)
                    .join(SPACES, SPACES.c.id == r.space_id)
                    .where(r.enabled.is_(True), c.enabled.is_(True)),
                    execution_options=self.options,
                )
            ).mappings()
            return [
                NotificationTargetRow(
                    rule=_notification_rule_row(row),
                    space_slug=row["space_slug"],
                    channel_config=dict(row["channel_config"]),
                    channel_secret=EncryptedPayload(
                        row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"]
                    ),
                )
                for row in rows
            ]

    async def enqueue_notifications(self, items: Sequence[NotificationEnqueue]) -> int:
        if not items:
            return 0
        total = 0
        async with self._own() as session:
            for item in items:
                values = {
                    "rule_id": item.rule_id,
                    "channel_id": item.channel_id,
                    "digest_key": item.digest_key,
                    "event_ids": [item.event_id],
                    "status": DeliveryStatus.PENDING,
                    "attempt": 0,
                    "scheduled_at": item.scheduled_at,
                }
                stmt = insert(NOTIFICATIONS).values(**values)
                if item.digest_key is not None:
                    event_ids = case(
                        (
                            func.array_position(NOTIFICATIONS.c.event_ids, item.event_id).is_(None),
                            func.array_append(NOTIFICATIONS.c.event_ids, item.event_id),
                        ),
                        else_=NOTIFICATIONS.c.event_ids,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[NOTIFICATIONS.c.digest_key],
                        index_where=and_(
                            NOTIFICATIONS.c.digest_key.is_not(None),
                            NOTIFICATIONS.c.status == DeliveryStatus.PENDING,
                        ),
                        set_={"event_ids": event_ids},
                    )
                else:
                    # повтор пачки потребителем: уведомление по этому правилу и событию уже есть
                    stmt = stmt.on_conflict_do_nothing()
                result = await _execute(session, stmt, execution_options=self.options)
                total += int(result.rowcount or 0)  # type: ignore[attr-defined]
        return total

    async def claim_notifications(self, *, limit: int, claim_for: timedelta) -> list[NotificationAttempt]:
        n, r, c = NOTIFICATIONS.c, NOTIFICATION_RULES.c, NOTIFICATION_CHANNELS.c
        async with self._own() as session:
            locked: list[int] = list(
                (
                    await _execute(
                        session,
                        select(n.id)
                        .join(NOTIFICATION_RULES, r.id == n.rule_id)
                        .join(NOTIFICATION_CHANNELS, c.id == n.channel_id)
                        .where(
                            n.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                            n.scheduled_at <= func.now(),
                            r.enabled.is_(True),
                            c.enabled.is_(True),
                            or_(c.paused_until.is_(None), c.paused_until <= func.now()),
                        )
                        .order_by(n.scheduled_at, n.id)
                        .limit(limit)
                        .with_for_update(of=NOTIFICATIONS, skip_locked=True),
                        execution_options=self.options,
                    )
                ).scalars()
            )
            if not locked:
                return []
            await _execute(
                session,
                update(NOTIFICATIONS)
                .where(n.id.in_(locked))
                # `failed` закрывает partial unique «open digest»: событие, пришедшее во время
                # сетевого I/O, создаст новое pending-уведомление и не потеряется.
                .values(
                    status=DeliveryStatus.FAILED,
                    attempt=n.attempt + 1,
                    scheduled_at=func.now() + claim_for,
                ),
                execution_options=self.options,
            )
            rows = list(
                (
                    await _execute(
                        session,
                        select(
                            n.id.label("notification_id"),
                            n.attempt,
                            n.rule_id,
                            n.channel_id,
                            n.event_ids,
                            r.template,
                            c.config.label("channel_config"),
                            c.key_id,
                            c.wrapped_dek,
                            c.nonce,
                            c.ciphertext,
                        )
                        .join(NOTIFICATION_RULES, r.id == n.rule_id)
                        .join(NOTIFICATION_CHANNELS, c.id == n.channel_id)
                        .where(n.id.in_(locked))
                        .order_by(n.id),
                        execution_options=self.options,
                    )
                ).mappings()
            )
            event_ids = {event_id for row in rows for event_id in row["event_ids"]}
            event_rows = (
                await _execute(
                    session,
                    select(EVENTS, SPACES.c.slug)
                    .join(SPACES, SPACES.c.id == EVENTS.c.space_id)
                    .where(EVENTS.c.event_id.in_(event_ids)),
                    execution_options=self.options,
                )
            ).mappings()
            events = {row["event_id"]: _event(row) for row in event_rows}
            return [
                NotificationAttempt(
                    notification_id=row["notification_id"],
                    attempt=row["attempt"],
                    rule_id=row["rule_id"],
                    channel_id=row["channel_id"],
                    template=row["template"],
                    channel_config=dict(row["channel_config"]),
                    channel_secret=EncryptedPayload(
                        row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"]
                    ),
                    events=tuple(events[event_id] for event_id in row["event_ids"] if event_id in events),
                )
                for row in rows
            ]

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
        n = NOTIFICATIONS.c
        async with self._own() as session:
            row = (
                await _execute(
                    session,
                    select(n.attempt, n.status, n.channel_id)
                    .where(n.id == notification_id)
                    .with_for_update(),
                    execution_options=self.options,
                )
            ).one_or_none()
            if (
                row is None
                or row.attempt != attempt
                or row.status not in (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
            ):
                return False
            values: dict[str, Any]
            if succeeded:
                values = {"status": DeliveryStatus.SUCCEEDED, "sent_at": func.now(), "error": None}
            elif pause_channel is not None:
                # 429: попытка не засчитывается, канал ждёт до конца паузы (R2-F03)
                values = {
                    "attempt": n.attempt - 1,
                    "scheduled_at": func.now() + pause_channel,
                    "error": error[:2000] if error else None,
                }
                c = NOTIFICATION_CHANNELS.c
                await _execute(
                    session,
                    update(NOTIFICATION_CHANNELS)
                    .where(c.id == row.channel_id)
                    .values(
                        paused_until=func.greatest(
                            func.coalesce(c.paused_until, func.now()), func.now() + pause_channel
                        )
                    ),
                    execution_options=self.options,
                )
            else:
                values = {
                    "status": DeliveryStatus.DEAD if attempt >= max_attempts else DeliveryStatus.FAILED,
                    "scheduled_at": func.now() + retry_after,
                    "error": error[:2000] if error else None,
                }
            await _execute(
                session,
                update(NOTIFICATIONS).where(n.id == notification_id).values(**values),
                execution_options=self.options,
            )
            return True

    # --------------------------------------------------------- обслуживание

    async def maintenance_spaces(self) -> list[SpaceRef]:
        async with self.sessionmaker() as session:
            rows = await _execute(
                session,
                select(SPACES.c.id, SPACES.c.slug, SPACES.c.title, SPACES.c.timezone).order_by(SPACES.c.slug),
                execution_options=self.options,
            )
        return [SpaceRef(row.id, row.slug, row.title, row.timezone) for row in rows]

    async def cleanup_maintenance(
        self, retention: Mapping[str, timedelta], names: frozenset[str], *, batch_size: int = 1000
    ) -> dict[str, int]:
        now = func.now()
        counts: dict[str, int] = {}

        def older(column: Any, name: str) -> Any:
            return column < now - retention[name]

        if "quota_windows" in names:
            counts["quota_windows"] = 0
            for seconds in await self._quota_window_sizes():
                # отдельно на каждый размер окна: так условие ложится на индекс (window_seconds, window_start)
                counts["quota_windows"] += await self._delete_batches(
                    QUOTAS,
                    [
                        QUOTAS.c.window_seconds == seconds,
                        QUOTAS.c.window_start <= now - timedelta(seconds=seconds),
                    ],
                    batch_size,
                )
        simple: dict[str, Callable[[], tuple[Table, list[Any]]]] = {
            "operation_keys": lambda: (OPERATION_KEYS, [OPERATION_KEYS.c.expires_at <= now]),
            "lease_logs": lambda: (LEASE_LOGS, [older(LEASE_LOGS.c.ts, "lease_logs")]),
            "webhook_deliveries": lambda: (
                WEBHOOK_DELIVERIES,
                [older(WEBHOOK_DELIVERIES.c.created_at, "deliveries")],
            ),
            "notifications": lambda: (
                NOTIFICATIONS,
                [older(func.coalesce(NOTIFICATIONS.c.sent_at, NOTIFICATIONS.c.scheduled_at), "deliveries")],
            ),
            "import_runs": lambda: (IMPORT_RUNS, [older(IMPORT_RUNS.c.finished_at, "import_runs")]),
            "provider_operations": lambda: (
                PROVIDER_OPERATIONS,
                [older(PROVIDER_OPERATIONS.c.finished_at, "provider_operations")],
            ),
            "metric_batches": lambda: (
                METRIC_BATCHES,
                [older(METRIC_BATCHES.c.created_at, "metric_batches")],
            ),
            "closed_challenges": lambda: (CHALLENGES, [older(CHALLENGES.c.resolved_at, "closed_challenges")]),
            "rollup_hours": lambda: (
                ROLLUPS,
                [
                    ROLLUPS.c.bucket_size == str(BucketSize.HOUR),
                    older(ROLLUPS.c.bucket_start, "rollup_hours"),
                ],
            ),
            "rollup_days": lambda: (
                ROLLUPS,
                [ROLLUPS.c.bucket_size == str(BucketSize.DAY), older(ROLLUPS.c.bucket_start, "rollup_days")],
            ),
        }
        for name, build in simple.items():
            if name in names:
                table, conditions = build()
                counts[name] = await self._delete_batches(table, conditions, batch_size)
        if names & {"lease_history", "events"}:
            # по пространствам: условие ложится на индексы (space_id, released_at) и (space_id, occurred_at)
            spaces = [space.id for space in await self.maintenance_spaces()]
            if "lease_history" in names:
                counts["lease_history"] = 0
                for space_id in spaces:
                    counts["lease_history"] += await self._delete_batches(
                        HISTORY,
                        [HISTORY.c.space_id == space_id, older(HISTORY.c.released_at, "lease_history")],
                        batch_size,
                    )
            if "events" in names:
                events = deliveries = 0
                for space_id in spaces:
                    removed = await self._delete_event_batches(
                        [EVENTS.c.space_id == space_id, older(EVENTS.c.occurred_at, "events")], batch_size
                    )
                    events += removed[0]
                    deliveries += removed[1]
                counts["events"] = events
                counts["webhook_deliveries"] = counts.get("webhook_deliveries", 0) + deliveries
        return counts

    async def _quota_window_sizes(self) -> list[int]:
        """Разные `window_seconds` в таблице квот: обход по индексу без чтения всех строк."""
        q = QUOTAS.c
        sizes = select(func.min(q.window_seconds).label("seconds")).cte("sizes", recursive=True)
        following = select(func.min(q.window_seconds)).where(q.window_seconds > sizes.c.seconds)
        sizes = sizes.union_all(select(following.scalar_subquery()).where(sizes.c.seconds.is_not(None)))
        async with self.sessionmaker() as session:
            rows = await _execute(
                session,
                select(sizes.c.seconds).where(sizes.c.seconds.is_not(None)),
                execution_options=self.options,
            )
            return [int(seconds) for seconds in rows.scalars()]

    @staticmethod
    def _batch(table: Table, conditions: Sequence[Any], batch_size: int) -> Any:
        """`DELETE` не больше `batch_size` строк, подходящих под `conditions`."""
        keys = list(table.primary_key.columns)
        key: Any = keys[0] if len(keys) == 1 else tuple_(*keys)
        return delete(table).where(key.in_(select(*keys).where(*conditions).limit(batch_size)))

    async def _delete_batches(self, table: Table, conditions: Sequence[Any], batch_size: int) -> int:
        """Удалять пачками, каждая в своей транзакции, пока подходящие строки не кончатся."""
        total = 0
        while True:
            async with self._own() as session:
                result: Any = await _execute(
                    session, self._batch(table, conditions, batch_size), execution_options=self.options
                )
                deleted = int(result.rowcount or 0)
            total += deleted
            if deleted < batch_size:
                return total

    async def _delete_event_batches(self, conditions: Sequence[Any], batch_size: int) -> tuple[int, int]:
        """События вместе с их доставками вебхуков: (удалено событий, удалено доставок)."""
        events = deliveries = 0
        while True:
            async with self._own() as session:
                event_ids = list(
                    (
                        await _execute(
                            session,
                            self._batch(EVENTS, conditions, batch_size).returning(EVENTS.c.event_id),
                            execution_options=self.options,
                        )
                    ).scalars()
                )
                if event_ids:
                    result: Any = await _execute(
                        session,
                        delete(WEBHOOK_DELIVERIES).where(WEBHOOK_DELIVERIES.c.event_id.in_(event_ids)),
                        execution_options=self.options,
                    )
                    deliveries += int(result.rowcount or 0)
            events += len(event_ids)
            if len(event_ids) < batch_size:
                return events, deliveries

    # --------------------------------------------------------- doctor / rotate-key

    async def stale_proxy_fingerprints(
        self, active_key_id: str, limit: int, after: UUID | None
    ) -> list[tuple[str, ProxyRow]]:
        p = PROXIES.c
        stmt = (
            select(PROXIES, SPACES.c.slug)
            .join(SPACES, SPACES.c.id == p.space_id)
            .where(or_(p.fingerprint_key_id.is_(None), p.fingerprint_key_id != active_key_id))
            .order_by(p.id)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(p.id > after)
        async with self.sessionmaker() as session:
            rows = (await _execute(session, stmt, execution_options=self.options)).mappings()
            return [(row["slug"], _proxy_row(row)) for row in rows]

    async def renew_leases(self, tokens: Sequence[UUID], ttl_seconds: float) -> dict[UUID, datetime]:
        if not tokens:
            return {}
        async with self._own() as session:
            rows = await _execute(
                session,
                update(LEASES)
                .where(_any_of(LEASES.c.token, list(tokens)), LEASES.c.expires_at > func.now())
                .values(
                    heartbeat_at=func.now(),
                    # продление никогда не укорачивает аренду (IMP-06, D-213)
                    expires_at=func.greatest(
                        LEASES.c.expires_at, func.now() + timedelta(seconds=ttl_seconds)
                    ),
                )
                .returning(LEASES.c.token, LEASES.c.expires_at),
                execution_options=self.options,
            )
            return {row.token: row.expires_at for row in rows}

    async def stale_normalizations(self, service: str, version: int, limit: int) -> list[tuple[UUID, str]]:
        i = IDENTITIES.c
        async with self.sessionmaker() as session:
            rows = await _execute(
                session,
                select(i.id, i.external_ref)
                .where(i.service == service, i.normalize_version != version)
                .order_by(i.id)
                .limit(limit),
                execution_options=self.options,
            )
            return [(row.id, row.external_ref) for row in rows]

    async def apply_normalizations(
        self, service: str, version: int, updates: Sequence[tuple[UUID, str]]
    ) -> list[list[UUID]]:
        if not updates:
            return []
        i = IDENTITIES.c
        ids = sorted(identity_id for identity_id, _ref in updates)
        async with self._own() as session:
            await _execute(
                session,
                select(i.id).where(_any_of(i.id, ids)).order_by(i.id).with_for_update(key_share=True),
                execution_options=self.options,
            )
            others = await _execute(
                session,
                select(i.id, i.normalized_ref).where(
                    i.service == service,
                    _any_of(i.normalized_ref, {ref for _id, ref in updates}),
                    i.id.not_in(ids),
                ),
                execution_options=self.options,
            )
            collisions = _normalization_collisions(updates, [(row.id, row.normalized_ref) for row in others])
            if collisions:
                return collisions
            # временные значения снимают конфликт уникальности при обмене ref между строками
            await _execute(
                session,
                update(IDENTITIES)
                .where(_any_of(i.id, ids))
                .values(normalized_ref=func.concat("~renormalize:", sa_cast(i.id, Text))),
                execution_options=self.options,
            )
            for identity_id, ref in updates:
                await _execute(
                    session,
                    update(IDENTITIES)
                    .where(i.id == identity_id)
                    .values(normalized_ref=ref, normalize_version=version),
                    execution_options=self.options,
                )
        return []

    async def doctor(self, *, fix: bool) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        after: UUID | None = None
        while True:
            # страницами по id: память и время линейны по числу аккаунтов (R2-D07)
            async with self._own() as session:
                page = select(IDENTITIES, SPACES.c.slug).join(SPACES, SPACES.c.id == IDENTITIES.c.space_id)
                if after is not None:
                    page = page.where(IDENTITIES.c.id > after)
                identities = list(
                    (
                        await _execute(
                            session,
                            page.order_by(IDENTITIES.c.id).limit(DOCTOR_PAGE),
                            execution_options=self.options,
                        )
                    ).mappings()
                )
                if not identities:
                    break
                after = identities[-1]["id"]
                issues += await self._doctor_page(session, identities, fix=fix)
            if len(identities) < DOCTOR_PAGE:
                break
        async with self._own() as session:
            binding_counts = (
                select(BINDINGS.c.proxy_id, func.count().label("n")).group_by(BINDINGS.c.proxy_id).subquery()
            )
            proxies = (
                await _execute(
                    session,
                    select(
                        PROXIES.c.id,
                        PROXIES.c.bound_count,
                        SPACES.c.slug,
                        func.coalesce(binding_counts.c.n, 0).label("expected"),
                    )
                    .join(SPACES, SPACES.c.id == PROXIES.c.space_id)
                    .outerjoin(binding_counts, binding_counts.c.proxy_id == PROXIES.c.id),
                    execution_options=self.options,
                )
            ).mappings()
            for row in proxies:
                if row["bound_count"] == row["expected"]:
                    continue
                issues.append(
                    DoctorIssue(
                        "proxy.bound_count",
                        row["slug"],
                        row["id"],
                        int(row["expected"]),
                        row["bound_count"],
                    )
                )
                if fix:
                    await _execute(
                        session,
                        update(PROXIES).where(PROXIES.c.id == row["id"]).values(bound_count=row["expected"]),
                        execution_options=self.options,
                    )
        return issues

    async def _doctor_page(
        self, session: AsyncSession, identities: list[Any], *, fix: bool
    ) -> list[DoctorIssue]:
        """Сверить денормализованные поля страницы аккаунтов с регионами и журналом событий."""
        ids = [row["id"] for row in identities]
        regions: dict[UUID, dict[tuple[str, str], Any]] = defaultdict(dict)
        region_rows = await _execute(
            session,
            select(REGION_STATES).where(_any_of(REGION_STATES.c.identity_id, ids)),
            execution_options=self.options,
        )
        for region in region_rows.mappings():
            regions[region["identity_id"]][(region["region"], region["key"])] = region
        event_rows = await _execute(
            session,
            select(
                EVENTS.c.identity_id,
                func.max(EVENTS.c.sequence).label("last_sequence"),
                func.max(EVENTS.c.revision).label("last_revision"),
            )
            .where(_any_of(EVENTS.c.identity_id, ids), EVENTS.c.sequence.is_not(None))
            .group_by(EVENTS.c.identity_id),
            execution_options=self.options,
        )
        event_seq: dict[UUID, int] = {}
        event_revision: dict[UUID, int] = {}
        for event_row in event_rows:
            event_seq[event_row.identity_id] = int(event_row.last_sequence)
            event_revision[event_row.identity_id] = int(event_row.last_revision or 0)
        issues: list[DoctorIssue] = []
        for row in identities:
            identity_id = row["id"]
            own = regions.get(identity_id, {})
            changes: dict[str, Any] = {}
            standing = own.get(("standing", ""))
            if standing is not None:
                expected = (standing["state"], standing["code"], standing["until"])
                actual = (row["standing"], row["standing_code"], row["standing_until"])
                if actual != expected:
                    issues.append(DoctorIssue("standing", row["slug"], identity_id, expected, actual))
                    changes.update(
                        standing=expected[0], standing_code=expected[1], standing_until=expected[2]
                    )
            auth = own.get(("auth", "default"))
            if auth is not None and row["auth_state"] != auth["state"]:
                issues.append(
                    DoctorIssue("auth_state", row["slug"], identity_id, auth["state"], row["auth_state"])
                )
                changes["auth_state"] = auth["state"]
            verified = sorted(
                key
                for (region, key), state in own.items()
                if region == "verification" and state["state"] == "verified"
            )
            if sorted(row["verified_factors"]) != verified:
                issues.append(
                    DoctorIssue(
                        "verified_factors", row["slug"], identity_id, verified, list(row["verified_factors"])
                    )
                )
                changes["verified_factors"] = verified
            expected_seq = event_seq.get(identity_id, 0)
            # После retention старые события отсутствуют, поэтому доказуем только нижнюю границу.
            if row["event_seq"] < expected_seq:
                issues.append(
                    DoctorIssue("event_seq", row["slug"], identity_id, expected_seq, row["event_seq"])
                )
                changes["event_seq"] = expected_seq
            expected_revision = event_revision.get(identity_id, 0)
            if row["revision"] < expected_revision:
                issues.append(
                    DoctorIssue("revision", row["slug"], identity_id, expected_revision, row["revision"])
                )
                changes["revision"] = expected_revision
            if fix and changes:
                await _execute(
                    session,
                    update(IDENTITIES).where(IDENTITIES.c.id == identity_id).values(**changes),
                    execution_options=self.options,
                )
        return issues

    def _rotation_specs(self) -> dict[str, tuple[Table, str, Callable[[Any], bytes]]]:
        return {
            "credentials": (
                CREDENTIALS,
                "",
                lambda r: credential_associated_data(r["identity_id"], r["kind"], r["name"]),
            ),
            "sessions": (
                SESSIONS,
                "",
                lambda r: associated_data("ih_sessions", r["identity_id"], r["slot"]),
            ),
            "challenges": (
                CHALLENGES,
                "context_",
                lambda r: associated_data("ih_challenges", r["identity_id"], str(r["id"])),
            ),
            "verifications": (
                VERIFICATIONS,
                "evidence_",
                lambda r: associated_data("ih_verifications", r["identity_id"], r["factor"]),
            ),
            "proxies": (PROXIES, "", lambda r: associated_data("ih_proxies", r["id"])),
            "webhooks": (
                WEBHOOK_ENDPOINTS,
                "",
                lambda r: associated_data("ih_webhook_endpoints", r["id"]),
            ),
            "notification_channels": (
                NOTIFICATION_CHANNELS,
                "",
                lambda r: associated_data("ih_notification_channels", r["id"]),
            ),
            "profiles": (
                IDENTITIES,
                "profile_",
                lambda r: associated_data("ih_identities", r["id"], "profile"),
            ),
            # незавершённый импорт: без него удаление старого ключа делает записи нечитаемыми (R2-S09)
            "import_items": (
                IMPORT_ITEMS,
                "",
                lambda r: associated_data("ih_import_items", r["run_id"], str(r["position"])),
            ),
        }

    async def rotate_key(
        self, envelope: Any, *, table: str, limit: int, after: tuple[Any, ...] | None = None
    ) -> RotationBatch:
        target, prefix, aad_of = self._rotation_specs()[table]
        active = envelope.active_key_id
        size = max(1, limit)
        primary = list(target.primary_key.columns)
        key = target.c[f"{prefix}key_id"]
        stmt = select(target).where(target.c[f"{prefix}ciphertext"].is_not(None), key != active)
        if after is not None:
            bound = [literal(value, type_=column.type) for column, value in zip(primary, after, strict=True)]
            stmt = stmt.where(tuple_(*primary) > tuple_(*bound))
        rotated = 0
        unreadable: list[UnreadableSecret] = []
        async with self._own() as session:
            rows = list(
                (
                    await _execute(
                        session,
                        # заблокированные строки пропускаются: их подберёт следующий проход (R2-S08)
                        stmt.order_by(*primary).limit(size).with_for_update(skip_locked=True),
                        execution_options=self.options,
                    )
                ).mappings()
            )
            for row in rows:
                payload = EncryptedPayload(
                    row[f"{prefix}key_id"],
                    row[f"{prefix}wrapped_dek"],
                    row[f"{prefix}nonce"],
                    row[f"{prefix}ciphertext"],
                )
                try:
                    # у challenge тем же DEK зашифрован ответ — формат AAD не меняем
                    renewed = envelope.rewrap(payload, aad_of(row), upgrade=table != "challenges")
                except DecryptionFailed:
                    label = (
                        credential_label(row["identity_id"], row["kind"], row["name"])
                        if table == "credentials"
                        else "/".join(str(row[column.name]) for column in primary)
                    )
                    unreadable.append(UnreadableSecret(table, label))
                    continue
                await _execute(
                    session,
                    update(target)
                    .where(*[column == row[column.name] for column in primary])
                    .values(
                        # nonce и шифротекст меняются, когда строка переходит на AAD v1 (IMP-16)
                        **renewed.columns(prefix)
                    ),
                    execution_options=self.options,
                )
                rotated += 1
        cursor = tuple(rows[-1][column.name] for column in primary) if len(rows) >= size else None
        return RotationBatch(rotated, tuple(unreadable), cursor)

    async def inactive_key_usage(self, active: str) -> dict[str, dict[str, int]]:
        usage: dict[str, dict[str, int]] = {}
        async with self.sessionmaker() as session:
            for name, (target, prefix, _aad) in self._rotation_specs().items():
                key = target.c[f"{prefix}key_id"]
                rows = await _execute(
                    session,
                    select(key, func.count())
                    .where(target.c[f"{prefix}ciphertext"].is_not(None), key != active)
                    .group_by(key),
                    execution_options=self.options,
                )
                counts = {str(row[0]): int(row[1]) for row in cast(Any, rows)}
                if counts:
                    usage[name] = counts
        return usage

    async def metrics_snapshot(self, slugs: frozenset[str] | None = None) -> MetricsSnapshot:
        cursor_lag: dict[str, int] = {}
        cursor_updated_at: dict[str, datetime] = {}
        async with self.sessionmaker() as session:
            cursors = await _execute(
                session,
                select(CURSORS.c.consumer, CURSORS.c.last_tx_id, CURSORS.c.last_id, CURSORS.c.updated_at),
                execution_options=self.options,
            )
            horizon = func.pg_snapshot_xmin(func.pg_current_snapshot())
            for cursor in cursors:
                lag = await _execute(
                    session,
                    select(func.count())
                    .select_from(EVENTS)
                    .where(
                        EVENTS.c.external.is_(True),
                        *(
                            [EVENTS.c.space_id.in_(select(SPACES.c.id).where(SPACES.c.slug.in_(slugs)))]
                            if slugs is not None
                            else []
                        ),
                        EVENTS.c.tx_id < horizon,
                        tuple_(EVENTS.c.tx_id, EVENTS.c.id)
                        > tuple_(sa_cast(literal(str(cursor.last_tx_id)), XID8()), cursor.last_id),
                    ),
                    execution_options=self.options,
                )
                cursor_lag[cursor.consumer] = int(lag.scalar_one())
                cursor_updated_at[cursor.consumer] = cursor.updated_at
            webhook_scope = [WEBHOOK_ENDPOINTS.c.space_id == SPACES.c.id]
            notification_scope = [NOTIFICATION_RULES.c.space_id == SPACES.c.id]
            if slugs is not None:
                webhook_scope.append(SPACES.c.slug.in_(slugs))
                notification_scope.append(SPACES.c.slug.in_(slugs))
            webhook = (
                await _execute(
                    session,
                    select(
                        func.count()
                        .filter(
                            WEBHOOK_DELIVERIES.c.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED])
                        )
                        .label("queued"),
                        func.count()
                        .filter(WEBHOOK_DELIVERIES.c.status == DeliveryStatus.FAILED)
                        .label("failed"),
                        func.min(WEBHOOK_DELIVERIES.c.next_attempt_at)
                        .filter(
                            WEBHOOK_DELIVERIES.c.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED])
                        )
                        .label("next_attempt_at"),
                        func.max(WEBHOOK_DELIVERIES.c.delivered_at).label("last_success_at"),
                        func.count().filter(WEBHOOK_DELIVERIES.c.error.is_not(None)).label("errors"),
                    )
                    .select_from(WEBHOOK_DELIVERIES)
                    .join(WEBHOOK_ENDPOINTS, WEBHOOK_ENDPOINTS.c.id == WEBHOOK_DELIVERIES.c.endpoint_id)
                    .join(SPACES, and_(*webhook_scope)),
                    execution_options=self.options,
                )
            ).one()
            notification = (
                await _execute(
                    session,
                    select(
                        func.count()
                        .filter(NOTIFICATIONS.c.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]))
                        .label("queued"),
                        func.count().filter(NOTIFICATIONS.c.status == DeliveryStatus.FAILED).label("failed"),
                        func.min(NOTIFICATIONS.c.scheduled_at)
                        .filter(NOTIFICATIONS.c.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]))
                        .label("next_attempt_at"),
                        func.max(NOTIFICATIONS.c.sent_at).label("last_success_at"),
                        func.count().filter(NOTIFICATIONS.c.error.is_not(None)).label("errors"),
                    )
                    .select_from(NOTIFICATIONS)
                    .join(NOTIFICATION_RULES, NOTIFICATION_RULES.c.id == NOTIFICATIONS.c.rule_id)
                    .join(SPACES, and_(*notification_scope)),
                    execution_options=self.options,
                )
            ).one()
        return MetricsSnapshot(
            cursor_lag,
            {"webhooks": int(webhook.queued), "notifications": int(notification.queued)},
            cursor_updated_at,
            {"webhooks": int(webhook.failed), "notifications": int(notification.failed)},
            {"webhooks": webhook.next_attempt_at, "notifications": notification.next_attempt_at},
            {"webhooks": webhook.last_success_at, "notifications": notification.last_success_at},
            {"webhooks": bool(webhook.errors), "notifications": bool(notification.errors)},
        )

    @asynccontextmanager
    async def listen(self, channel: str) -> AsyncIterator[asyncio.Event | None]:
        """LISTEN на отдельном соединении; `None` — уведомлений нет, вызывающий работает опросом.

        Подписка проверяется пробным NOTIFY с другого соединения: через PgBouncer в режиме
        транзакций LISTEN «проходит», но уведомления не доходят (R2-D21). Тогда соединение
        сразу возвращается в пул, а в лог пишется, что диспетчер работает в режиме опроса.
        """
        engine = self.sessionmaker.kw.get("bind")
        if engine is None:
            yield None
            return
        signal = asyncio.Event()

        def notified(*_: Any) -> None:
            signal.set()

        conn: Any = None
        driver: Any = None
        reason = "драйвер не поддерживает LISTEN"
        try:
            conn = await engine.connect()
            raw = await conn.get_raw_connection()
            candidate = raw.driver_connection
            if candidate is not None and hasattr(candidate, "add_listener"):
                if await self._notifications_arrive(engine, candidate):
                    await candidate.add_listener(channel, notified)
                    driver = candidate
                else:
                    reason = "пробное уведомление не дошло (PgBouncer в режиме транзакций?)"
        except Exception as exc:
            reason = describe_error(exc)
        if driver is None:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()
            _logger.info("LISTEN недоступен: %s; диспетчер работает в режиме опроса", reason)
            yield None
            return
        try:
            yield signal
        finally:
            with contextlib.suppress(Exception):
                await driver.remove_listener(channel, notified)
            await conn.close()

    @staticmethod
    async def _notifications_arrive(engine: Any, driver: Any) -> bool:
        """Доходит ли до этого соединения NOTIFY, отправленный с другого соединения пула."""
        probe = f"ih_listen_probe_{uuid4().hex}"
        arrived = asyncio.Event()

        def got(*_: Any) -> None:
            arrived.set()

        await driver.add_listener(probe, got)
        try:
            async with engine.begin() as other:
                await other.execute(text("SELECT pg_notify(:channel, '')"), {"channel": probe})
            try:
                await asyncio.wait_for(arrived.wait(), timeout=LISTEN_PROBE_TIMEOUT)
            except TimeoutError:
                return False
            return True
        finally:
            with contextlib.suppress(Exception):
                await driver.remove_listener(probe, got)

    async def ensure_space(
        self,
        slug: str,
        title: str | None = None,
        space_id: UUID | None = None,
        *,
        timezone: str = "UTC",
    ) -> SpaceRef:
        stmt = (
            insert(SpaceRecord)
            .values(id=space_id or new_id(), slug=slug, title=title or slug, timezone=timezone)
            .on_conflict_do_nothing(index_elements=[SpaceRecord.slug])
        )
        async with self._own() as session:
            await _execute(session, stmt, execution_options=self.options)
        found = await self.get_space(slug)
        if found is None:  # pragma: no cover - вставка или существующая строка гарантированы
            raise RuntimeError(f"пространство {slug} не создано")
        if space_id is not None and found.id != space_id:
            raise SpaceMismatch(f"пространство {slug!r} уже существует с другим id")
        return found


def _normalization_collisions(
    updates: Sequence[tuple[UUID, str]], others: Sequence[tuple[UUID, str]]
) -> list[list[UUID]]:
    """Группы id, которые после перенормализации получили бы один ref (IMP-08)."""
    groups: dict[str, list[UUID]] = {}
    for identity_id, ref in [*others, *updates]:
        groups.setdefault(ref, []).append(identity_id)
    return [sorted(ids) for _ref, ids in sorted(groups.items()) if len(ids) > 1]


def _rollup_row(row: Any) -> RollupRow:
    return RollupRow(
        RollupKey(
            row["space_id"],
            row["service"],
            row["metric"],
            tuple(sorted(row["dims"].items())),
            row["bucket_start"],
            BucketSize(row["bucket_size"]),
        ),
        float(row["value"]),
    )
