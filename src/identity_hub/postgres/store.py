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
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Table, and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Result
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Executable

from identity_hub._ids import new_id
from identity_hub.crypto.envelope import EncryptedPayload
from identity_hub.domain.regions import RegionState
from identity_hub.enums import ChangeSource
from identity_hub.errors import SpaceMismatch
from identity_hub.events import EventDraft
from identity_hub.postgres import schema_options
from identity_hub.postgres.models import (
    CredentialsRecord,
    EventRecord,
    IdentityDataRecord,
    IdentityLinkRecord,
    IdentityRecord,
    RegionStateRecord,
    ServiceRecord,
    SessionRecord,
    SpaceRecord,
)
from identity_hub.store.base import (
    DataRow,
    IdentityRow,
    NewIdentity,
    ServiceRow,
    SpaceRef,
    Store,
    StoreTx,
)

__all__ = ["NOTIFY_CHANNEL", "PgStore", "PgTx"]

IDENTITIES = cast(Table, IdentityRecord.__table__)
REGION_STATES = cast(Table, RegionStateRecord.__table__)
CREDENTIALS = cast(Table, CredentialsRecord.__table__)
SESSIONS = cast(Table, SessionRecord.__table__)
LINKS = cast(Table, IdentityLinkRecord.__table__)
DATA = cast(Table, IdentityDataRecord.__table__)
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
