"""IMP-07 (I1.2a): миграция 0017 — backfill ревизии из `event_seq`, ревизия в событиях (D-205)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres.migrate import alembic_config, migrate

pytestmark = [pytest.mark.pg]


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_rev_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def _seed(db: AsyncEngine, schema: str, event_seqs: list[int]) -> list[str]:
    space_id = uuid4()
    ids = [str(uuid4()) for _ in event_seqs]
    async with db.begin() as conn:
        await conn.execute(text(f"INSERT INTO \"{schema}\".ih_services (name, title) VALUES ('svc', 'Svc')"))
        await conn.execute(
            text(f'INSERT INTO "{schema}".ih_spaces (id, slug, title) VALUES (:id, :slug, :slug)'),
            {"id": space_id, "slug": f"s{uuid4().hex[:6]}"},
        )
        for n, (identity_id, seq) in enumerate(zip(ids, event_seqs, strict=True)):
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_identities '
                    "(id, space_id, service, external_ref, normalized_ref, lifecycle, standing, auth_state, "
                    "availability, error_score, fencing_seq, event_seq) "
                    "VALUES (:id, :space, 'svc', :ref, :ref, "
                    "'provisioning', 'good', 'no_session', 'available', 0, 0, :seq)"
                ),
                {"id": identity_id, "space": space_id, "ref": f"+{n}", "seq": seq},
            )
            await conn.execute(
                text(
                    f'INSERT INTO "{schema}".ih_events '
                    "(event_id, type, severity, space_id, identity_id, sequence, actor, external) "
                    "VALUES (:event_id, 'identity.registered', 'info', :space, :id, :seq, 'test', true)"
                ),
                {"event_id": uuid4(), "space": space_id, "id": identity_id, "seq": seq},
            )
    return ids


async def test_backfill_keeps_seen_revisions(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0016")
    ids = await _seed(db, schema, [0, 1, 7])

    await migrate(db, db_schema=schema)

    async with db.connect() as conn:
        revisions = dict(
            (str(row.id), row.revision)
            for row in await conn.execute(text(f'SELECT id, revision FROM "{schema}".ih_identities'))
        )
        events = (await conn.execute(text(f'SELECT revision FROM "{schema}".ih_events'))).scalars().all()
    # клиенты видели event_seq как revision: значения не уменьшаются, минимум — 1
    assert revisions == {ids[0]: 1, ids[1]: 1, ids[2]: 7}
    assert events == [None, None, None]


async def test_downgrade_drops_revision_columns(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema)

    async with db.begin() as conn:
        await conn.run_sync(lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0016"))

    async with db.connect() as conn:
        columns = (
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema = :schema AND column_name = 'revision'"
                    ),
                    {"schema": schema},
                )
            )
            .scalars()
            .all()
        )
    assert columns == []
