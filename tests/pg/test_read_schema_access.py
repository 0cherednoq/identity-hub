"""C8.2 (`D-326`): credentials и sessions в read-схеме — отбор непроверенных и аренда по нему."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate, IdentityHub, Lifecycle
from identity_hub.postgres.read_schema import READ_SCHEMA_VERSION, ReadSchema

pytestmark = pytest.mark.pg

CREDENTIAL_COLUMNS = {
    "id", "identity_id", "space_id", "kind", "name", "public_value", "lifecycle", "expires_at",
    "last_verified_at", "last_rejected_at", "reject_code", "revision", "created_at", "updated_at",
}  # fmt: skip

SESSION_COLUMNS = {
    "id", "identity_id", "space_id", "slot", "status", "expires_at", "last_verified_at",
    "last_rejected_at", "reject_code", "revision", "created_at", "updated_at", "revoked_at",
}  # fmt: skip

SECRET_COLUMNS = {
    "payload", "key_id", "wrapped_dek", "nonce", "ciphertext", "value_fingerprint", "version",
    "written_by_fencing",
}  # fmt: skip


def test_projections_expose_metadata_without_secrets() -> None:
    read = ReadSchema()
    assert set(read.credentials.c.keys()) == CREDENTIAL_COLUMNS
    assert set(read.sessions.c.keys()) == SESSION_COLUMNS
    for table in (read.credentials, read.sessions):
        assert not {column.name for column in table.c} & SECRET_COLUMNS
    # добавление таблиц совместимо: версия не меняется (`D-326`)
    assert READ_SCHEMA_VERSION == 5


async def test_lease_among_unverified_credentials(hub: IdentityHub, db: AsyncEngine) -> None:
    space = hub.space(f"read-unver-{uuid4().hex[:6]}", create=True)
    ref = await space.ref()
    fresh, checked = [
        (
            await space.identities.create(
                IdentityCreate(
                    service="fake_tg",
                    external_ref=f"{space.slug}-{index}",
                    credentials={"phone": f"+{index}", "password": "p"},
                    session={"auth_key": "k"},
                    lifecycle=Lifecycle.READY,
                ),
                actor="test",
            )
        ).id
        for index in range(2)
    ]
    await space.secrets.verify_credential(checked, actor="ops")

    read = hub.read
    unverified = select(read.credentials.c.identity_id).where(
        read.credentials.c.space_id == ref.id,
        read.credentials.c.lifecycle == "active",
        read.credentials.c.last_verified_at.is_(None),
        read.credentials.c.last_rejected_at.is_(None),
    )
    async with db.connect() as conn:
        assert set((await conn.execute(unverified)).scalars()) == {fresh}

    pool = space.pools.define("checkers", service="fake_tg")
    async with pool.lease(among=unverified) as lease:
        assert lease.identity_id == fresh


async def test_sessions_projection_joins_identities(hub: IdentityHub, db: AsyncEngine) -> None:
    space = hub.space(f"read-sess-{uuid4().hex[:6]}", create=True)
    ref = await space.ref()
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref=f"{space.slug}-s",
            credentials={"phone": "+9", "password": "secret-pass"},
            session={"auth_key": "secret-key"},
            lifecycle=Lifecycle.READY,
        ),
        actor="test",
    )
    await space.secrets.reject_session(identity.id, actor="ops", code="session_invalid")

    read = hub.read
    stmt = (
        select(
            read.identities.c.external_ref,
            read.services.c.url,
            read.credentials.c.kind,
            read.sessions.c.status,
            read.sessions.c.reject_code,
        )
        .join(read.services, read.services.c.name == read.identities.c.service)
        .join(read.credentials, read.credentials.c.identity_id == read.identities.c.id, isouter=True)
        .join(read.sessions, read.sessions.c.identity_id == read.identities.c.id, isouter=True)
        .where(read.identities.c.space_id == ref.id)
    )
    async with db.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    assert len(rows) == 1 and rows[0]["reject_code"] == "session_invalid"
    assert rows[0]["status"] == "active" and rows[0]["url"]
    dumped = repr([dict(row) for row in rows])
    assert "secret-pass" not in dumped and "secret-key" not in dumped
