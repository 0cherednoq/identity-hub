"""PostgreSQL-реализация хранилища. Весь SQL библиотеки живёт здесь (D-008).

Правила (ТЗ §7, §8):
- своя транзакция — `sessionmaker()` + `begin()`; сессия пользователя — только `begin_nested()` (T-1);
- `set_config('ih.space', slug, true)` в начале каждой транзакции (T-3);
- таблицы — только Core-конструкции с `schema_translate_map` (D-013), без text() с именами таблиц.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import fields
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Table,
    Text,
    and_,
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Executable, Select

from identity_hub._ids import new_id
from identity_hub.crypto.envelope import EncryptedPayload
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
)
from identity_hub.errors import LeaseUnavailable, SpaceMismatch
from identity_hub.events import Event, EventDraft
from identity_hub.postgres import schema_options
from identity_hub.postgres.models import (
    BindingRecord,
    ChallengeRecord,
    CredentialsRecord,
    EventCursorRecord,
    EventRecord,
    IdentityDataRecord,
    IdentityLinkRecord,
    IdentityRecord,
    LeaseHistoryRecord,
    LeaseLogRecord,
    LeaseRecord,
    NotificationChannelRecord,
    NotificationRecord,
    NotificationRuleRecord,
    OperationKeyRecord,
    ProxyRecord,
    QuotaUsageRecord,
    RegionStateRecord,
    ServiceRecord,
    SessionRecord,
    SpaceRecord,
    VerificationRecord,
    WebhookDeliveryRecord,
    WebhookEndpointRecord,
)
from identity_hub.store.base import (
    NOTIFY_CHANNEL,
    CandidateQuery,
    ChallengeRow,
    ChargeOutcome,
    DataRow,
    IdentityRow,
    LeaseCounters,
    LeaseHistoryRow,
    LeaseRow,
    LeaseStatsRow,
    LinkRow,
    NewIdentity,
    NewNotificationChannel,
    NewNotificationRule,
    NewProxy,
    NewWebhookEndpoint,
    NotificationAttempt,
    NotificationChannelRow,
    NotificationEnqueue,
    NotificationRuleRow,
    NotificationTargetRow,
    ProxyRow,
    QuotaCharge,
    QuotaWindow,
    SearchFilter,
    ServiceRow,
    SpaceRef,
    Store,
    StoreTx,
    VerificationRow,
    WebhookAttempt,
    WebhookDeliveryRow,
    WebhookEndpointRow,
)

__all__ = ["NOTIFY_CHANNEL", "PgStore", "PgTx"]

IDENTITIES = cast(Table, IdentityRecord.__table__)
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
PROXIES = cast(Table, ProxyRecord.__table__)
BINDINGS = cast(Table, BindingRecord.__table__)
CHALLENGES = cast(Table, ChallengeRecord.__table__)
VERIFICATIONS = cast(Table, VerificationRecord.__table__)
EVENTS = cast(Table, EventRecord.__table__)
CURSORS = cast(Table, EventCursorRecord.__table__)
SPACES = cast(Table, SpaceRecord.__table__)
WEBHOOK_ENDPOINTS = cast(Table, WebhookEndpointRecord.__table__)
WEBHOOK_DELIVERIES = cast(Table, WebhookDeliveryRecord.__table__)
NOTIFICATION_CHANNELS = cast(Table, NotificationChannelRecord.__table__)
NOTIFICATION_RULES = cast(Table, NotificationRuleRecord.__table__)
NOTIFICATIONS = cast(Table, NotificationRecord.__table__)
PROXY_FIELDS = tuple(f.name for f in fields(ProxyRow))
LEASE_HISTORY_FIELDS = tuple(f.name for f in fields(LeaseHistoryRow))
IDENTITY_FIELDS = tuple(f.name for f in fields(IdentityRow))
REGION_FIELDS = tuple(f.name for f in fields(RegionState))

SESSION_SPACE_KEY = "identity_hub.space"


class PgTx(StoreTx):
    def __init__(self, store: PgStore, session: AsyncSession, space: SpaceRef) -> None:
        self.store = store
        self.session = session
        self.space = space
        self._now: datetime | None = None

    async def execute(self, stmt: Executable, params: Any = None) -> Result[Any]:
        return await self.session.execute(stmt, params, execution_options=self.store.options)

    async def now(self) -> datetime:
        if self._now is None:
            self._now = (await self.session.execute(select(func.now()))).scalar_one()
        return self._now

    async def emit(self, drafts: Sequence[EventDraft]) -> list[UUID]:
        if not drafts:
            return []
        rows: list[dict[str, Any]] = []
        for draft in drafts:
            sequence = None
            if draft.identity_id is not None:
                sequence = (
                    await self.execute(
                        update(IdentityRecord)
                        .where(
                            IdentityRecord.id == draft.identity_id, IdentityRecord.space_id == self.space.id
                        )
                        .values(event_seq=IdentityRecord.event_seq + 1)
                        .returning(IdentityRecord.event_seq)
                    )
                ).scalar_one_or_none()
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
                    "actor": draft.actor,
                    "data": dict(draft.data),
                    "external": draft.spec.external,
                }
            )
        await self.execute(insert(EventRecord).values(rows))
        if any(r["external"] for r in rows):
            # доставляется слушателям при коммите; при откате — нет (FR-13.6)
            await self.session.execute(
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
        self, identity_id: UUID, service: str, external_ref: str
    ) -> tuple[UUID, UUID] | None:
        row = (
            await self.execute(
                select(IDENTITIES.c.id, IDENTITIES.c.space_id)
                .where(
                    or_(
                        IDENTITIES.c.id == identity_id,
                        and_(IDENTITIES.c.service == service, IDENTITIES.c.external_ref == external_ref),
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
                IDENTITIES.c.id, IDENTITIES.c.space_id, IDENTITIES.c.service, IDENTITIES.c.external_ref
            ).where(tuple_(IDENTITIES.c.service, IDENTITIES.c.external_ref).in_(pairs))
        )
        return {(r.service, r.external_ref): (r.id, r.space_id) for r in rows}

    async def existing_identities(self, ids: Sequence[UUID]) -> set[UUID]:
        if not ids:
            return set()
        rows = await self.execute(
            select(IDENTITIES.c.id).where(
                IDENTITIES.c.id.in_(list(ids)), IDENTITIES.c.space_id == self.space.id
            )
        )
        return set(rows.scalars())

    async def put_credentials(self, identity_id: UUID, payload: EncryptedPayload) -> None:
        stmt = insert(CREDENTIALS).values(
            identity_id=identity_id, space_id=self.space.id, **payload.columns()
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[CREDENTIALS.c.identity_id],
            set_={**payload.columns(), "version": CREDENTIALS.c.version + 1, "updated_at": func.now()},
        )
        await self.execute(stmt)

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
            .where(LINKS.c.identity_id.in_(id_list), LINKS.c.space_id == self.space.id)
            .group_by(LINKS.c.identity_id)
            .subquery()
        )
        incoming = (
            select(LINKS.c.linked_identity_id.label("id"), func.count().label("n"))
            .where(LINKS.c.linked_identity_id.in_(id_list), LINKS.c.space_id == self.space.id)
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
            conditions.append(or_(lease.identity_id.is_(None), lease.expires_at <= now))
        if query.proxy_wait:
            down = (
                select(BINDINGS.c.identity_id)
                .join(PROXIES, PROXIES.c.id == BINDINGS.c.proxy_id)
                .where(BINDINGS.c.identity_id == i.id, PROXIES.c.status != ProxyStatus.ALIVE)
            )
            conditions.append(~down.exists())
        return conditions

    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
        i, lease = IDENTITIES.c, LEASES.c
        conditions = self._candidate_conditions(query)
        stmt = (
            select(IDENTITIES)
            .select_from(IDENTITIES.outerjoin(LEASES, lease.identity_id == i.id))
            .where(*conditions)
            .order_by(lease.acquired_at.asc().nulls_first(), i.id)
            .limit(1)
            .with_for_update(of=IDENTITIES, skip_locked=True)
        )
        row = (await self.execute(stmt)).mappings().one_or_none()
        return IdentityRow(**{f: row[f] for f in IDENTITY_FIELDS}) if row else None

    async def count_candidates(self, query: CandidateQuery, limit: int) -> int:
        i, lease = IDENTITIES.c, LEASES.c
        capped = (
            select(i.id)
            .select_from(IDENTITIES.outerjoin(LEASES, lease.identity_id == i.id))
            .where(*self._candidate_conditions(query))
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
        fencing: int = (
            await self.execute(
                update(IDENTITIES)
                .where(IDENTITIES.c.id == identity.id, IDENTITIES.c.space_id == self.space.id)
                .values(fencing_seq=IDENTITIES.c.fencing_seq + 1)
                .returning(IDENTITIES.c.fencing_seq)
            )
        ).scalar_one()
        lease_id, token = new_id(), uuid4()
        reclaimed: LeaseRow | None = None
        if not shared:
            if idempotency_key is not None:
                # ключ истёкшей аренды освобождается, иначе уникальный индекс не даст его переиспользовать
                await self.execute(
                    update(LEASES)
                    .where(LEASES.c.idempotency_key == idempotency_key, LEASES.c.expires_at <= func.now())
                    .values(idempotency_key=None)
                )
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
        )
        return lease, reclaimed

    async def extend_lease(
        self, lease: LeaseRow, ttl_seconds: float, counters: LeaseCounters, *, shared: bool
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
                    .values(heartbeat_at=func.now(), expires_at=func.now() + timedelta(seconds=ttl_seconds))
                    .returning(LEASES.c.expires_at)
                )
            ).scalar_one_or_none()
            if expires_at is None:
                return None
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
    ) -> bool:
        if not shared:
            await self.execute(
                update(LEASES)
                .where(
                    LEASES.c.identity_id == lease.identity_id,
                    LEASES.c.token == lease.token,
                    LEASES.c.expires_at > func.now(),
                )
                .values(expires_at=func.now(), idempotency_key=None)
            )
        closed = await self.execute(
            update(HISTORY)
            .where(
                HISTORY.c.lease_id == lease.lease_id,
                HISTORY.c.token == lease.token,
                HISTORY.c.released_at.is_(None),
            )
            .values(released_at=func.now(), end_reason=reason, **_counter_values(counters))
        )
        return bool(closed.rowcount)  # type: ignore[attr-defined]

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
        row = (
            await self.execute(
                select(
                    CREDENTIALS.c.key_id,
                    CREDENTIALS.c.wrapped_dek,
                    CREDENTIALS.c.nonce,
                    CREDENTIALS.c.ciphertext,
                ).where(CREDENTIALS.c.identity_id == identity_id, CREDENTIALS.c.space_id == self.space.id)
            )
        ).one_or_none()
        return EncryptedPayload(row.key_id, row.wrapped_dek, row.nonce, row.ciphertext) if row else None

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

    # --------------------------------------------------------- квоты

    async def charge_quotas(
        self, identity_id: UUID, charges: Sequence[QuotaCharge], *, operation_key: str | None, kind: str
    ) -> ChargeOutcome:
        savepoint = await self.session.begin_nested()
        try:
            if operation_key is not None:
                claimed = await self.execute(
                    insert(OPERATION_KEYS)
                    .values(key=operation_key, identity_id=identity_id, space_id=self.space.id, kind=kind)
                    .on_conflict_do_nothing()
                )
                if not claimed.rowcount:  # type: ignore[attr-defined]
                    await savepoint.rollback()
                    return ChargeOutcome(duplicate=True)
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
            return ChargeOutcome(reached=tuple(reached))
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

    async def finish_operation_key(self, key: str, outcome: str) -> None:
        await self.execute(
            update(OPERATION_KEYS)
            .where(OPERATION_KEYS.c.key == key, OPERATION_KEYS.c.space_id == self.space.id)
            .values(outcome=outcome)
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

    async def adjust_proxy_load(self, proxy_id: UUID, delta: int) -> None:
        await self.execute(
            update(PROXIES)
            .where(PROXIES.c.id == proxy_id)
            .values(bound_count=func.greatest(0, PROXIES.c.bound_count + delta))
        )

    async def due_proxies(self, limit: int) -> list[UUID]:
        rows = await self.execute(
            select(PROXIES.c.id)
            .where(
                PROXIES.c.space_id == self.space.id,
                PROXIES.c.status == ProxyStatus.QUARANTINED,
                PROXIES.c.next_check_at <= func.now(),
            )
            .order_by(PROXIES.c.next_check_at)
            .limit(limit)
        )
        return list(rows.scalars())

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
            .values(resolved_at=func.now())
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
                IDENTITIES.c.lifecycle != Lifecycle.RETIRED,
                or_(synced.is_(None), synced < threshold),
            )
            .order_by(synced.asc().nulls_first(), IDENTITIES.c.id)
            .limit(limit)
        )
        return list(rows.scalars())

    # --------------------------------------------------------- поиск

    async def search_identities(
        self,
        flt: SearchFilter,
        *,
        limit: int,
        after: tuple[datetime, UUID] | None,
        count_limit: int | None,
    ) -> tuple[list[IdentityRow], int | None]:
        i = IDENTITIES.c
        conditions: list[Any] = [i.space_id == self.space.id]
        if flt.exact_id is not None:
            conditions.append(i.id == flt.exact_id)
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
        page = select(IDENTITIES).where(*conditions)
        if after is not None:
            page = page.where(tuple_(i.created_at, i.id) < tuple_(literal(after[0]), literal(after[1])))
        page = page.order_by(i.created_at.desc(), i.id.desc()).limit(limit)
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
        return bool(result.rowcount)  # type: ignore[attr-defined]

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
                .order_by(NOTIFICATION_RULES.c.updated_at, NOTIFICATION_RULES.c.id)
            )
        ).mappings()
        return [_notification_rule_row(row) for row in rows]

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
    )


def cast_uuid_array(ids: list[UUID]) -> Any:
    return sa_cast(ids, ARRAY(PGUUID(as_uuid=True)))


class PgStore(Store):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], db_schema: str | None = None) -> None:
        self.sessionmaker = sessionmaker
        self.db_schema = db_schema
        self.options: dict[str, Any] = schema_options(db_schema)

    @asynccontextmanager
    async def _own(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session, session.begin():
            yield session

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
        async with session.begin_nested():
            await self._bind(session, space)
            yield PgTx(self, session, space)

    @staticmethod
    async def _bind(session: AsyncSession, space: SpaceRef) -> None:
        await session.execute(text("SELECT set_config('ih.space', :slug, true)"), {"slug": space.slug})

    # --------------------------------------------------------- реестр и пространства

    async def sync_services(self, services: Sequence[ServiceRow]) -> None:
        if not services:
            return
        stmt = insert(ServiceRecord).values([{"name": s.name, "title": s.title} for s in services])
        stmt = stmt.on_conflict_do_update(
            index_elements=[ServiceRecord.name],
            set_={"title": stmt.excluded.title, "synced_at": func.now()},
        )
        async with self._own() as session:
            await session.execute(stmt, execution_options=self.options)

    async def get_space(self, slug: str) -> SpaceRef | None:
        async with self.sessionmaker() as session:
            row = (
                await session.execute(
                    select(SpaceRecord.id, SpaceRecord.slug, SpaceRecord.title).where(
                        SpaceRecord.slug == slug
                    ),
                    execution_options=self.options,
                )
            ).one_or_none()
        return SpaceRef(row.id, row.slug, row.title) if row else None

    # --------------------------------------------------------- журнал событий

    async def process_events(
        self,
        consumer: str,
        handler: Callable[[list[Event]], Awaitable[None]],
        *,
        limit: int,
    ) -> int | None:
        opts = self.options
        async with self._own() as session:
            await session.execute(
                insert(CURSORS).values(consumer=consumer, last_id=0).on_conflict_do_nothing(),
                execution_options=opts,
            )
        async with self._own() as session:
            locked = (
                await session.execute(
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
            stmt = (
                select(e, SPACES.c.slug)
                .join(SPACES, SPACES.c.id == e.space_id)
                .join(cursor, tuple_(e.tx_id, e.id) > tuple_(cursor.c.last_tx_id, cursor.c.last_id))
                .where(
                    e.external.is_(True),
                    e.tx_id < horizon,
                )
                .order_by(e.tx_id, e.id)
                .limit(limit)
            )
            rows = list((await session.execute(stmt, execution_options=opts)).mappings())
            if not rows:
                return 0
            events = [_event(r) for r in rows]
            await handler(events)
            last_id = rows[-1]["id"]
            await session.execute(
                update(CURSORS)
                .where(CURSORS.c.consumer == consumer)
                .values(
                    last_tx_id=select(e.tx_id).where(e.id == last_id).scalar_subquery(),
                    last_id=last_id,
                    updated_at=func.now(),
                ),
                execution_options=opts,
            )
            return len(events)

    # --------------------------------------------------------- исходящие вебхуки

    async def enabled_webhook_endpoints(self) -> list[WebhookEndpointRow]:
        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
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
            rows = await session.execute(
                select(IDENTITIES.c.id, IDENTITIES.c.labels).where(IDENTITIES.c.id.in_(set(identity_ids))),
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
            result = await session.execute(stmt, execution_options=self.options)
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def claim_webhook_deliveries(self, *, limit: int, claim_for: timedelta) -> list[WebhookAttempt]:
        d, ep, e = WEBHOOK_DELIVERIES.c, WEBHOOK_ENDPOINTS.c, EVENTS.c
        async with self._own() as session:
            locked: list[int] = list(
                (
                    await session.execute(
                        select(d.id)
                        .join(WEBHOOK_ENDPOINTS, ep.id == d.endpoint_id)
                        .where(
                            d.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                            d.next_attempt_at <= func.now(),
                            ep.enabled.is_(True),
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
            await session.execute(
                update(WEBHOOK_DELIVERIES)
                .where(d.id.in_(locked))
                .values(attempt=d.attempt + 1, next_attempt_at=func.now() + claim_for),
                execution_options=self.options,
            )
            rows = (
                await session.execute(
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
                await session.execute(
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
            if (
                row is None
                or row.attempt != attempt
                or row.status not in (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
            ):
                return False
            common = {
                "response_code": response_code,
                "response_ms": response_ms,
                "error": error[:2000] if error else None,
            }
            if succeeded:
                await session.execute(
                    update(WEBHOOK_DELIVERIES)
                    .where(d.id == delivery_id)
                    .values(**common, status=DeliveryStatus.SUCCEEDED, delivered_at=func.now()),
                    execution_options=self.options,
                )
                await session.execute(
                    update(WEBHOOK_ENDPOINTS).where(ep.id == row.endpoint_id).values(failure_streak=0),
                    execution_options=self.options,
                )
                return True

            streak = row.failure_streak + 1
            disabled = streak >= max_failures
            await session.execute(
                update(WEBHOOK_DELIVERIES)
                .where(d.id == delivery_id)
                .values(
                    **common,
                    status=DeliveryStatus.DEAD if disabled else DeliveryStatus.FAILED,
                    next_attempt_at=func.now() + retry_after,
                ),
                execution_options=self.options,
            )
            await session.execute(
                update(WEBHOOK_ENDPOINTS)
                .where(ep.id == row.endpoint_id)
                .values(
                    failure_streak=streak,
                    enabled=False if disabled else row.enabled,
                    disabled_reason="consecutive_failures" if disabled else None,
                ),
                execution_options=self.options,
            )
            if disabled:
                await session.execute(
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
                await session.execute(
                    text("SELECT pg_notify(:channel, :payload)"),
                    {"channel": NOTIFY_CHANNEL, "payload": row.slug},
                )
            return True

    # --------------------------------------------------------- Telegram-уведомления

    async def enabled_notification_targets(self) -> list[NotificationTargetRow]:
        r, c = NOTIFICATION_RULES.c, NOTIFICATION_CHANNELS.c
        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
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
                result = await session.execute(stmt, execution_options=self.options)
                total += int(result.rowcount or 0)  # type: ignore[attr-defined]
        return total

    async def claim_notifications(self, *, limit: int, claim_for: timedelta) -> list[NotificationAttempt]:
        n, r, c = NOTIFICATIONS.c, NOTIFICATION_RULES.c, NOTIFICATION_CHANNELS.c
        async with self._own() as session:
            locked: list[int] = list(
                (
                    await session.execute(
                        select(n.id)
                        .join(NOTIFICATION_RULES, r.id == n.rule_id)
                        .join(NOTIFICATION_CHANNELS, c.id == n.channel_id)
                        .where(
                            n.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                            n.scheduled_at <= func.now(),
                            r.enabled.is_(True),
                            c.enabled.is_(True),
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
            await session.execute(
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
                    await session.execute(
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
                await session.execute(
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
    ) -> bool:
        n = NOTIFICATIONS.c
        async with self._own() as session:
            row = (
                await session.execute(
                    select(n.attempt, n.status).where(n.id == notification_id).with_for_update(),
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
            else:
                values = {
                    "status": DeliveryStatus.DEAD if attempt >= max_attempts else DeliveryStatus.FAILED,
                    "scheduled_at": func.now() + retry_after,
                    "error": error[:2000] if error else None,
                }
            await session.execute(
                update(NOTIFICATIONS).where(n.id == notification_id).values(**values),
                execution_options=self.options,
            )
            return True

    @asynccontextmanager
    async def listen(self, channel: str) -> AsyncIterator[asyncio.Event | None]:
        engine = self.sessionmaker.kw.get("bind")
        signal = asyncio.Event()
        if engine is None:
            yield None
            return
        try:
            conn = await engine.connect()
        except Exception:
            yield None
            return
        try:
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection
            if driver is None or not hasattr(driver, "add_listener"):
                yield None
                return

            def notified(*_: Any) -> None:
                signal.set()

            try:
                await driver.add_listener(channel, notified)
            except Exception:  # PgBouncer в transaction mode и т.п. — режим опроса (ТЗ §8.7)
                yield None
                return
            try:
                yield signal
            finally:
                with contextlib.suppress(Exception):
                    await driver.remove_listener(channel, notified)
        finally:
            await conn.close()

    async def ensure_space(
        self, slug: str, title: str | None = None, space_id: UUID | None = None
    ) -> SpaceRef:
        stmt = (
            insert(SpaceRecord)
            .values(id=space_id or new_id(), slug=slug, title=title or slug)
            .on_conflict_do_nothing(index_elements=[SpaceRecord.slug])
        )
        async with self._own() as session:
            await session.execute(stmt, execution_options=self.options)
        found = await self.get_space(slug)
        if found is None:  # pragma: no cover - вставка или существующая строка гарантированы
            raise RuntimeError(f"пространство {slug} не создано")
        if space_id is not None and found.id != space_id:
            raise SpaceMismatch(f"пространство {slug!r} уже существует с другим id")
        return found
