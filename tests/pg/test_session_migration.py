"""IMP-10 (I3.3a): миграция 0023 — статусы сессий и история слота без потери шифртекста (D-218)."""

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
    name = f"ih_sess_{uuid4().hex[:10]}"
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
                "(:id, :space, 'svc', '+1', '+1', 'ready', 'good', 'authenticated', 'available', 0, 0, 0)"
            ),
            {"id": identity_id, "space": space_id},
        )
        for slot, status in (("default", "valid"), ("web", "invalid"), ("old", "expired")):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_sessions '
                    "(identity_id, slot, space_id, status, version, written_by_fencing, key_id, wrapped_dek, "
                    "nonce, ciphertext) VALUES (:id, :slot, :space, :status, 2, 1, 'k1', :dek, :nonce, :ct)"
                ),
                {
                    "id": identity_id,
                    "slot": slot,
                    "space": space_id,
                    "status": status,
                    "dek": b"dek",
                    "nonce": b"nonce",
                    "ct": slot.encode(),
                },
            )
    return identity_id


async def test_statuses_mapped_and_ciphertext_kept(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0022")
    await _seed(db, schema)

    await migrate(db, db_schema=schema)

    async with db.connect() as conn:
        rows = {
            row.slot: row
            for row in await conn.execute(
                text(f'SELECT slot, status, revision, id, ciphertext, version FROM "{schema}".ih_sessions')
            )
        }
    assert {slot: row.status for slot, row in rows.items()} == {
        "default": "active",
        "web": "invalidated",
        "old": "invalidated",
    }
    assert all(row.id is not None and row.revision == 1 and row.version == 2 for row in rows.values())
    assert {slot: bytes(row.ciphertext) for slot, row in rows.items()} == {
        "default": b"default",
        "web": b"web",
        "old": b"old",
    }


async def test_downgrade_keeps_current_records(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0022")
    identity_id = await _seed(db, schema)
    await migrate(db, db_schema=schema)
    async with db.begin() as conn:  # отозванная запись истории
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_sessions '
                "(id, identity_id, slot, space_id, status, version, revision, written_by_fencing, key_id, "
                "wrapped_dek, nonce, ciphertext) SELECT gen_random_uuid(), "
                f"identity_id, slot, space_id, 'revoked', 1, 0, 0, key_id, wrapped_dek, nonce, ciphertext "
                f'FROM "{schema}".ih_sessions WHERE identity_id = :id AND slot = :slot'
            ),
            {"id": identity_id, "slot": "default"},
        )

    async with db.begin() as conn:
        await conn.run_sync(lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0022"))

    async with db.connect() as conn:
        statuses = sorted((await conn.execute(text(f'SELECT status FROM "{schema}".ih_sessions'))).scalars())
    assert statuses == ["invalid", "invalid", "valid"]
