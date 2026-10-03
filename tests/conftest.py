"""Общие фикстуры: PostgreSQL 16 (testcontainers или IH_TEST_DATABASE_URL).

`IH_TEST_PGBOUNCER=1` — `IH_TEST_DATABASE_URL` указывает на PgBouncer в режиме транзакций:
engine создаётся по рецепту из docs/integrations.md, а миграции идут по прямому адресу
`IH_TEST_MIGRATE_DATABASE_URL` (сессионная блокировка `migrate` через PgBouncer не работает).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub, SpaceHandle

# учёт вызовов методов Store при полном прогоне (F5.3b): IH_STORE_COVERAGE=1
pytest_plugins = ["tests.store_coverage"] if os.environ.get("IH_STORE_COVERAGE") == "1" else []


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("IH_TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # старые версии testcontainers
        from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


def pgbouncer_connect_args() -> dict[str, Any]:
    """Рецепт asyncpg для PgBouncer в режиме транзакций (docs/integrations.md, R2-D04)."""
    return {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    }


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    connect_args = pgbouncer_connect_args() if os.environ.get("IH_TEST_PGBOUNCER") == "1" else {}
    eng = create_async_engine(database_url, pool_size=10, max_overflow=40, connect_args=connect_args)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture(scope="session")
async def prepared_db(engine: AsyncEngine) -> AsyncEngine:
    """Схема библиотеки в public, накатывается миграциями один раз на прогон."""
    from identity_hub.postgres.migrate import migrate

    direct = os.environ.get("IH_TEST_MIGRATE_DATABASE_URL")
    if direct is None:
        await migrate(engine)
        return engine
    migrator = create_async_engine(direct)
    try:
        await migrate(migrator)
    finally:
        await migrator.dispose()
    return engine


@pytest.fixture
async def db(prepared_db: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Чистая БД на каждый тест: TRUNCATE всех таблиц библиотеки перед тестом."""
    from identity_hub.postgres import metadata

    tables = ", ".join(t.name for t in metadata.sorted_tables)
    async with prepared_db.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield prepared_db


TEST_KEYS = {"k1": b"\x11" * 32, "k2": b"\x22" * 32}


@pytest.fixture
def sessionmaker(db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db, expire_on_commit=False)


@pytest.fixture
async def hub(sessionmaker: async_sessionmaker[AsyncSession]) -> IdentityHub:
    from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

    instance = IdentityHub(
        sessionmaker,
        StaticSecretsProvider(TEST_KEYS, "k1"),
        [FAKE_TG, FAKE_EXCHANGE],
        admin_base_url="https://admin.test",
        strict_events=True,
    )
    await instance.start()
    return instance


@pytest.fixture
def space(hub: IdentityHub) -> SpaceHandle:
    return hub.space("tennet", "Tennet", create=True)
