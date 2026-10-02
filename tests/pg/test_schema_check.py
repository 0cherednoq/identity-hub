"""IMP-15 (I0.4): проверка ревизии схемы при старте и advisory lock миграции.

Каждый тест работает в собственной схеме PostgreSQL, чтобы не трогать общую тестовую БД.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from identity_hub import IdentityHub, SchemaIncompatible, StaticSecretsProvider
from identity_hub.postgres.migrate import (
    MIGRATION_LOCK_NAMESPACE,
    SCHEMA_COMPATIBLE,
    SCHEMA_HEAD,
    VERSION_TABLE,
    alembic_config,
    migrate,
)
from tests.conftest import TEST_KEYS
from tests.fixtures.api import register
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


# ---------------------------------------------------------------- F4.3: совместимые ревизии (R1-17)

_OLDEST_COMPATIBLE = min(SCHEMA_COMPATIBLE)
_REVISIONS = [script.revision for script in ScriptDirectory.from_config(alembic_config()).walk_revisions()]


def test_compatible_revisions_are_a_tail_of_history_ending_at_head() -> None:
    """Код совместим с непрерывным хвостом истории: дыр и ревизий старше/новее истории нет."""
    history = sorted(_REVISIONS)
    assert SCHEMA_HEAD in SCHEMA_COMPATIBLE
    assert sorted(SCHEMA_COMPATIBLE) == history[history.index(_OLDEST_COMPATIBLE) :]


@pytest.mark.parametrize("revision", sorted(SCHEMA_COMPATIBLE))
async def test_compatible_revision_starts_and_works(db: AsyncEngine, schema: str, revision: str) -> None:
    """R1-17: код запускается и работает на любой ревизии из `SCHEMA_COMPATIBLE` — выкат без простоя."""
    await migrate(db, db_schema=schema, revision=revision)
    hub = _hub(db, schema)

    await hub.start()

    space = hub.space("compat", "Compat")
    reg = await register(
        space, service="fake_tg", external_ref="+100", lifecycle="ready", session={"auth_key": "k"}
    )
    lease = await space.pools.define("p", service="fake_tg").acquire()
    assert lease.identity_id == reg.identity_id
    await lease.release()
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/compat", secret=SecretStr("s"), event_types=["identity.updated"]
    )
    await space.webhooks.set_enabled(endpoint.id, False, reason="test")
    report = await hub.maintenance.run_all()
    assert report.counts["leases"] == 0


async def test_revision_older_than_compatible_points_to_migrate(db: AsyncEngine, schema: str) -> None:
    history = sorted(_REVISIONS)
    too_old = history[history.index(_OLDEST_COMPATIBLE) - 1]
    await migrate(db, db_schema=schema, revision=too_old)

    with pytest.raises(SchemaIncompatible) as exc:
        await _hub(db, schema).start()

    assert exc.value.actual == too_old
    assert "identity-hub migrate" in str(exc.value)


async def test_revision_newer_than_code_points_to_package_upgrade(db: AsyncEngine, schema: str) -> None:
    """Схема новее кода: `migrate` не поможет, нужно обновить пакет."""
    await migrate(db, db_schema=schema)
    await _set_versions(db, schema, ["9999"])

    with pytest.raises(SchemaIncompatible) as exc:
        await _hub(db, schema).start()

    message = str(exc.value)
    assert "identity-hub migrate" not in message and "обновите" in message


async def test_missing_schema_points_to_migrate(db: AsyncEngine, schema: str) -> None:
    async with db.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    with pytest.raises(SchemaIncompatible) as exc:
        await _hub(db, schema).start()

    assert "identity-hub migrate" in str(exc.value)
