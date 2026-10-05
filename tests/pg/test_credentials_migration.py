"""IMP-09 (I3.1a): миграция 0022 — прежняя строка credentials становится default без изменения шифртекста."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres.migrate import alembic_config, migrate

pytestmark = [pytest.mark.anyio, pytest.mark.pg]


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_cred_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def _seed(db: AsyncEngine, schema: str) -> str:
    space_id, identity_id = uuid4(), str(uuid4())
    async with db.begin() as conn:
        await conn.execute(text(f"INSERT INTO \"{schema}\".ih_services (name, title) VALUES ('svc', 'Svc')"))
        await conn.execute(
            text(f'INSERT INTO "{schema}".ih_spaces (id, slug, title) VALUES (:id, :slug, :slug)'),
            {"id": space_id, "slug": "s"},
        )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_identities (id, space_id, service, external_ref, normalized_ref, '
                "lifecycle, standing, auth_state, availability, error_score, fencing_seq, event_seq) VALUES "
                "(:id, :space, 'svc', '+1', '+1', 'ready', 'good', 'no_session', 'available', 0, 0, 0)"
            ),
            {"id": identity_id, "space": space_id},
        )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_credentials '
                "(identity_id, space_id, version, key_id, wrapped_dek, nonce, ciphertext) "
                "VALUES (:id, :space, 3, 'k1', :dek, :nonce, :ct)"
            ),
            {"id": identity_id, "space": space_id, "dek": b"dek", "nonce": b"nonce", "ct": b"cipher"},
        )
    return identity_id


async def test_legacy_row_becomes_default_credential(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0021")
    identity_id = await _seed(db, schema)

    await migrate(db, db_schema=schema)

    async with db.connect() as conn:
        row = (await conn.execute(text(f'SELECT * FROM "{schema}".ih_credentials'))).mappings().one()
    assert (str(row["identity_id"]), row["kind"], row["name"], row["lifecycle"]) == (
        identity_id,
        "default",
        "default",
        "active",
    )
    assert row["id"] is not None and row["version"] == 3 and row["revision"] == 1
    assert (row["key_id"], row["wrapped_dek"], row["nonce"], row["ciphertext"]) == (
        "k1",
        b"dek",
        b"nonce",
        b"cipher",
    )


async def test_downgrade_keeps_default_and_refuses_extra_kinds(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0021")
    identity_id = await _seed(db, schema)
    await migrate(db, db_schema=schema)

    async with db.begin() as conn:
        await conn.run_sync(lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0021"))
    async with db.connect() as conn:
        count = (await conn.execute(text(f'SELECT count(*) FROM "{schema}".ih_credentials'))).scalar_one()
    assert count == 1

    await migrate(db, db_schema=schema)
    async with db.begin() as conn:
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_credentials '
                "(id, identity_id, space_id, kind, name, version, key_id, wrapped_dek, nonce, ciphertext) "
                "SELECT gen_random_uuid(), identity_id, space_id, 'api_key', "
                "'trading', 1, key_id, wrapped_dek, nonce, ciphertext FROM "
                f'"{schema}".ih_credentials WHERE identity_id = :id'
            ),
            {"id": identity_id},
        )
    with pytest.raises(Exception, match="credentials кроме default"):
        async with db.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0021")
            )
