"""IMP-08 (I1.1): миграция 0008 — backfill normalized_ref и остановка на коллизиях."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres.migrate import VERSION_TABLE, migrate

pytestmark = [pytest.mark.anyio, pytest.mark.pg]


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_norm_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def _seed(db: AsyncEngine, schema: str, refs: list[str]) -> list[str]:
    space_id = uuid4()
    ids = [str(uuid4()) for _ in refs]
    async with db.begin() as conn:
        await conn.execute(text(f"INSERT INTO \"{schema}\".ih_services (name, title) VALUES ('svc', 'Svc')"))
        await conn.execute(
            text(f'INSERT INTO "{schema}".ih_spaces (id, slug, title) VALUES (:id, :slug, :slug)'),
            {"id": space_id, "slug": f"s{uuid4().hex[:6]}"},
        )
        for identity_id, ref in zip(ids, refs, strict=True):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_identities '
                    "(id, space_id, service, external_ref, lifecycle, standing, auth_state, availability, "
                    "error_score, fencing_seq, event_seq) "
                    "VALUES (:id, :space, 'svc', :ref, "
                    "'provisioning', 'good', 'no_session', 'available', 0, 0, 0)"
                ),
                {"id": identity_id, "space": space_id, "ref": ref},
            )
    return ids


async def _version(db: AsyncEngine, schema: str) -> str:
    async with db.connect() as conn:
        return str(
            (await conn.execute(text(f'SELECT version_num FROM "{schema}".{VERSION_TABLE}'))).scalar_one()
        )


async def test_backfill_strips_whitespace(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0007")
    ids = await _seed(db, schema, ["  +1 ", "user@mail.com"])

    await migrate(db, db_schema=schema)

    async with db.connect() as conn:
        rows = dict(
            (str(row.id), (row.normalized_ref, row.normalize_version))
            for row in await conn.execute(
                text(f'SELECT id, normalized_ref, normalize_version FROM "{schema}".ih_identities')
            )
        )
    assert rows == {ids[0]: ("+1", 0), ids[1]: ("user@mail.com", 0)}


async def test_migration_collision_stops_without_changes(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0007")
    ids = await _seed(db, schema, ["+12025550123", " +12025550123 ", "+19999999999"])

    with pytest.raises(RuntimeError) as exc:
        await migrate(db, db_schema=schema)

    message = str(exc.value)
    assert ids[0] in message and ids[1] in message and ids[2] not in message
    assert "+1202555" not in message  # в сообщении только id, без телефонов
    assert await _version(db, schema) == "0007"
    async with db.connect() as conn:
        columns = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = 'ih_identities' "
                "AND column_name = 'normalized_ref'"
            ),
            {"schema": schema},
        )
        assert columns.first() is None
