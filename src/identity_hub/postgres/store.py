"""PostgreSQL-реализация хранилища. Весь SQL библиотеки живёт здесь (D-008).

Правила (ТЗ §7, §8):
- своя транзакция — `sessionmaker()` + `begin()`; сессия пользователя — только `begin_nested()` (T-1);
- `set_config('ih.space', slug, true)` в начале каждой транзакции (T-3);
- таблицы — только Core-конструкции с `schema_translate_map` (D-013), без text() с именами таблиц.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
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
from identity_hub.enums import AuthState, Availability, ChangeSource, LeaseEndReason, Lifecycle, ProxyStatus
from identity_hub.errors import LeaseUnavailable, SpaceMismatch
from identity_hub.events import EventDraft
from identity_hub.postgres import schema_options
from identity_hub.postgres.models import (
    BindingRecord,
    ChallengeRecord,
    CredentialsRecord,
    EventRecord,
    IdentityDataRecord,
    IdentityLinkRecord,
    IdentityRecord,
    LeaseHistoryRecord,
    LeaseRecord,
    OperationKeyRecord,
    ProxyRecord,
    QuotaUsageRecord,
    RegionStateRecord,
    ServiceRecord,
    SessionRecord,
    SpaceRecord,
    VerificationRecord,
)
from identity_hub.store.base import (
    CandidateQuery,
    ChallengeRow,
    ChargeOutcome,
    DataRow,
    IdentityRow,
    LeaseCounters,
    LeaseRow,
    LinkRow,
    NewIdentity,
    NewProxy,
    ProxyRow,
    QuotaCharge,
    QuotaWindow,
    SearchFilter,
    ServiceRow,
    SpaceRef,
    Store,
    StoreTx,
    VerificationRow,
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
QUOTAS = cast(Table, QuotaUsageRecord.__table__)
OPERATION_KEYS = cast(Table, OperationKeyRecord.__table__)
PROXIES = cast(Table, ProxyRecord.__table__)
BINDINGS = cast(Table, BindingRecord.__table__)
CHALLENGES = cast(Table, ChallengeRecord.__table__)
VERIFICATIONS = cast(Table, VerificationRecord.__table__)
PROXY_FIELDS = tuple(f.name for f in fields(ProxyRow))
IDENTITY_FIELDS = tuple(f.name for f in fields(IdentityRow))
REGION_FIELDS = tuple(f.name for f in fields(RegionState))

SESSION_SPACE_KEY = "identity_hub.space"
NOTIFY_CHANNEL = "ih_events"


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

    async def pick_candidate(self, query: CandidateQuery) -> IdentityRow | None:
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
