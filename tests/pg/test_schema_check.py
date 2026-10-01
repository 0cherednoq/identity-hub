"""IMP-15 (I0.4): проверка ревизии схемы при старте и advisory lock миграции.

Каждый тест работает в собственной схеме PostgreSQL, чтобы не трогать общую тестовую БД.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from identity_hub import IdentityHub, SchemaIncompatible, StaticSecretsProvider
from identity_hub.postgres.migrate import (
    MIGRATION_LOCK_NAMESPACE,
    SCHEMA_HEAD,
    VERSION_TABLE,
    alembic_config,
    migrate,
)
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_TG

pytestmark = [pytest.mark.anyio, pytest.mark.pg]


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_schema_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


def _hub(db: AsyncEngine, schema: str) -> IdentityHub:
    return IdentityHub(
        async_sessionmaker(db, expire_on_commit=False),
        StaticSecretsProvider(TEST_KEYS, "k1"),
        [FAKE_TG],
        db_schema=schema,
    )


async def _set_versions(db: AsyncEngine, schema: str, versions: list[str]) -> None:
    async with db.begin() as conn:
        await conn.execute(text(f'DELETE FROM "{schema}".{VERSION_TABLE}'))
        for version in versions:
            await conn.execute(
                text(f'INSERT INTO "{schema}".{VERSION_TABLE} (version_num) VALUES (:v)'), {"v": version}
            )


def test_schema_head_matches_alembic_head() -> None:
    assert ScriptDirectory.from_config(alembic_config()).get_heads() == [SCHEMA_HEAD]


async def test_head_revision_starts(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema)
    await _hub(db, schema).start()


async def test_old_revision_rejected(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema, revision="0005")

    with pytest.raises(SchemaIncompatible) as exc:
        await _hub(db, schema).start()

    assert exc.value.context == {"expected": SCHEMA_HEAD, "actual": "0005"}
    assert "postgresql" not in str(exc.value).lower() and schema not in str(exc.value)


@pytest.mark.parametrize(
    ("versions", "actual"),
    [(["9999"], "9999"), (["0006", "0007"], "0006, 0007")],
    ids=["newer", "branched"],
)
async def test_unknown_or_branched_revision_rejected(
    db: AsyncEngine, schema: str, versions: list[str], actual: str
) -> None:
    await migrate(db, db_schema=schema)
    await _set_versions(db, schema, versions)

    with pytest.raises(SchemaIncompatible) as exc:
        await _hub(db, schema).start()
    assert exc.value.actual == actual


async def test_missing_schema_rejected(db: AsyncEngine, schema: str) -> None:
    async with db.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    with pytest.raises(SchemaIncompatible) as exc:
        await _hub(db, schema).start()
    assert exc.value.actual is None


async def test_concurrent_migrate_is_serialized(db: AsyncEngine, schema: str) -> None:
    await asyncio.wait_for(
        asyncio.gather(migrate(db, db_schema=schema), migrate(db, db_schema=schema)), timeout=120
    )
    await _hub(db, schema).start()


async def test_regular_work_does_not_take_migration_lock(db: AsyncEngine, schema: str) -> None:
    await migrate(db, db_schema=schema)
    hub = _hub(db, schema)
    async with db.connect() as holder:
        transaction = await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": f"{MIGRATION_LOCK_NAMESPACE}:{schema}"},
        )
        try:
            await asyncio.wait_for(hub.start(), timeout=10)
            await asyncio.wait_for(
                hub.space("lockless").identities.find(service="fake_tg", external_ref="+none"), timeout=10
            )
        finally:
            await transaction.rollback()
