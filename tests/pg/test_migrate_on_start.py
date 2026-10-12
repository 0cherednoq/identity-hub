"""C6.1: режим миграций при старте хаба — `check`, `auto`, `off`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from identity_hub import IdentityHub, SchemaIncompatible, StaticSecretsProvider, ValidationFailed
from identity_hub.postgres.migrate import SCHEMA_HEAD, VERSION_TABLE, migrate
from identity_hub.testing import MemoryStore
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


@pytest.fixture
async def schema(db: AsyncEngine) -> AsyncIterator[str]:
    name = f"ih_start_{uuid4().hex[:10]}"
    yield name
    async with db.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


def _hub(db: AsyncEngine, schema: str, **options: str) -> IdentityHub:
    return IdentityHub(
        async_sessionmaker(db, expire_on_commit=False),
        StaticSecretsProvider(TEST_KEYS, "k1"),
        [FAKE_TG],
        db_schema=schema,
        **options,  # type: ignore[arg-type]
    )


async def _version(db: AsyncEngine, schema: str) -> str | None:
    async with db.connect() as conn:
        exists = await conn.execute(text("SELECT to_regclass(:name)"), {"name": f"{schema}.{VERSION_TABLE}"})
        if exists.scalar() is None:
            return None
        return (await conn.execute(text(f'SELECT version_num FROM "{schema}".{VERSION_TABLE}'))).scalar()


async def test_check_is_default_and_does_not_migrate(db: AsyncEngine, schema: str) -> None:
    """C6.1: по умолчанию `start()` только сверяет ревизию и схему не создаёт."""
    with pytest.raises(SchemaIncompatible):
        await _hub(db, schema).start()
    assert await _version(db, schema) is None


async def test_auto_migrates_empty_and_outdated_schema(db: AsyncEngine, schema: str) -> None:
    """C6.1: `auto` применяет миграции на пустой схеме и доводит устаревшую до текущей ревизии."""
    hub = _hub(db, schema, migrate="auto")
    await hub.start()
    assert await _version(db, schema) == SCHEMA_HEAD
    space = hub.space("main", create=True)
    await space.proxies.create("http://10.20.0.1:80", group="g")  # схемой можно пользоваться

    await _hub(db, schema, migrate="auto").start()  # повторный старт ничего не ломает
    assert await _version(db, schema) == SCHEMA_HEAD


async def test_auto_upgrades_outdated_schema(db: AsyncEngine, schema: str) -> None:
    """C6.1: схема на прежней ревизии при `auto` обновляется, а при `check` — отказ."""
    await migrate(db, db_schema=schema, revision="0020")
    with pytest.raises(SchemaIncompatible):
        await _hub(db, schema).start()

    await _hub(db, schema, migrate="auto").start()

    assert await _version(db, schema) == SCHEMA_HEAD


@pytest.mark.parametrize("attempt", range(3))
async def test_two_auto_starts_at_once(db: AsyncEngine, schema: str, attempt: int) -> None:
    """C6.1: два одновременных старта на пустой базе оба завершаются успешно."""
    await asyncio.wait_for(
        asyncio.gather(_hub(db, schema, migrate="auto").start(), _hub(db, schema, migrate="auto").start()),
        timeout=90,
    )
    assert await _version(db, schema) == SCHEMA_HEAD


async def test_off_skips_revision_check(db: AsyncEngine, schema: str) -> None:
    """C6.1: `off` не сверяет ревизию: старт проходит и на схеме, которую `check` отклоняет."""
    await migrate(db, db_schema=schema)
    async with db.begin() as conn:
        await conn.execute(text(f"UPDATE \"{schema}\".{VERSION_TABLE} SET version_num = '0005'"))
    with pytest.raises(SchemaIncompatible):
        await _hub(db, schema).start()

    await _hub(db, schema, migrate="off").start()


async def test_migrate_mode_is_validated(db: AsyncEngine, schema: str) -> None:
    """C6.1: неизвестный режим и `auto` без PostgreSQL отклоняются при создании хаба."""
    with pytest.raises(ValidationFailed):
        _hub(db, schema, migrate="always")
    with pytest.raises(ValidationFailed):
        IdentityHub(secrets=StaticSecretsProvider(TEST_KEYS, "k1"), store=MemoryStore(), migrate="auto")
    memory = IdentityHub(secrets=StaticSecretsProvider(TEST_KEYS, "k1"), store=MemoryStore(), migrate="off")
    await memory.start()
