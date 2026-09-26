"""Общие фикстуры: PostgreSQL 16 (testcontainers или IH_TEST_DATABASE_URL)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub, SpaceHandle


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


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url, pool_size=10, max_overflow=40)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture(scope="session")
async def prepared_db(engine: AsyncEngine) -> AsyncEngine:
    """Схема библиотеки в public, накатывается миграциями один раз на прогон."""
    from identity_hub.postgres.migrate import migrate

    await migrate(engine)
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
    return hub.space("tennet", "Tennet")
