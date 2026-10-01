"""F1.1 (IMP-03): миграция 0009 — ключи идемпотентности по `(namespace, key)`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub.postgres.migrate import VERSION_TABLE, alembic_config, migrate

pytestmark = pytest.mark.pg


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_idem_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


async def _identity(db: AsyncEngine, schema: str) -> tuple[UUID, UUID]:
    space_id, identity_id = uuid4(), uuid4()
    async with db.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO \"{schema}\".ih_services (name, title) VALUES ('svc', 'Svc') "
                "ON CONFLICT DO NOTHING"
            )
        )
        await conn.execute(
            text(f'INSERT INTO "{schema}".ih_spaces (id, slug, title) VALUES (:id, :slug, :slug)'),
            {"id": space_id, "slug": f"s{uuid4().hex[:6]}"},
        )
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_identities '
                "(id, space_id, service, external_ref, normalized_ref, lifecycle, standing, auth_state, "
                "availability, error_score, fencing_seq, event_seq) "
                "VALUES (:id, :space, 'svc', :ref, :ref, "
                "'provisioning', 'good', 'no_session', 'available', 0, 0, 0)"
            ),
            {"id": identity_id, "space": space_id, "ref": uuid4().hex},
        )
    return space_id, identity_id


async def test_upgrade_moves_operation_keys_into_space_namespace(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0008")
    space_id, identity_id = await _identity(db, schema)
    async with db.begin() as conn:
        await conn.execute(
            text(
                f'INSERT INTO "{schema}".ih_operation_keys (key, identity_id, space_id, kind, outcome) '
                "VALUES ('job:1', :identity, :space, 'send_message', 'succeeded')"
            ),
            {"identity": identity_id, "space": space_id},
        )

    await migrate(db, db_schema=schema)

    async with db.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT namespace, key, operation, outcome, fingerprint_version, fingerprint, attempt, "
                    f'expires_at - created_at AS ttl FROM "{schema}".ih_operation_keys'
                )
            )
        ).one()
    assert (row.namespace, row.key, row.operation, row.outcome) == (
        f"{space_id}:operation",
        "job:1",
        "send_message",
        "succeeded",
    )
    assert (row.fingerprint_version, row.fingerprint, row.attempt) == (0, None, 1)
    assert row.ttl.days == 7


async def test_same_key_fits_two_namespaces_after_upgrade(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema)
    insert = text(
        f'INSERT INTO "{schema}".ih_operation_keys '
        "(namespace, key, space_id, operation, fingerprint_version, attempt, expires_at) "
        "VALUES (:namespace, 'order-123', :space, 'send', 1, 1, now() + interval '1 day')"
    )
    first, second = uuid4(), uuid4()
    async with db.begin() as conn:
        await conn.execute(insert, {"namespace": f"{first}:operation", "space": first})
        await conn.execute(insert, {"namespace": f"{second}:operation", "space": second})
    with pytest.raises(IntegrityError):
        async with db.begin() as conn:
            await conn.execute(insert, {"namespace": f"{first}:operation", "space": first})


async def test_downgrade_restores_global_key(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema)
    first_space, first_identity = await _identity(db, schema)
    second_space, second_identity = await _identity(db, schema)
    insert = text(
        f'INSERT INTO "{schema}".ih_operation_keys '
        "(namespace, key, space_id, identity_id, operation, fingerprint_version, attempt, created_at, "
        "expires_at) VALUES (:namespace, :key, :space, :identity, 'send', 1, 1, "
        "now() - make_interval(secs => :age), now() + interval '1 day')"
    )
    async with db.begin() as conn:
        for space, identity, age in ((first_space, first_identity, 60), (second_space, second_identity, 0)):
            await conn.execute(
                insert,
                {
                    "namespace": f"{space}:operation",
                    "key": "order-123",
                    "space": space,
                    "identity": identity,
                    "age": age,
                },
            )
        await conn.execute(
            insert,
            {
                "namespace": f"{first_space}:lease",
                "key": "l",
                "space": first_space,
                "identity": None,
                "age": 0,
            },
        )

    async with db.begin() as conn:
        await conn.run_sync(lambda sync_conn: command.downgrade(alembic_config(schema, sync_conn), "0008"))

    async with db.connect() as conn:
        version = (await conn.execute(text(f'SELECT version_num FROM "{schema}".{VERSION_TABLE}'))).scalar()
        rows = (
            await conn.execute(text(f'SELECT key, space_id, kind FROM "{schema}".ih_operation_keys'))
        ).all()
    assert version == "0008"
    assert [(row.key, row.space_id, row.kind) for row in rows] == [("order-123", second_space, "send")]
