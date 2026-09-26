"""Общие фикстуры: PostgreSQL 16 (testcontainers или IH_TEST_DATABASE_URL)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


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
    """Схема библиотеки в public, создаётся один раз на прогон (после P0.4 — миграциями)."""
    from identity_hub.postgres import metadata

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    return engine


@pytest.fixture
async def db(prepared_db: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Чистая БД на каждый тест: TRUNCATE всех таблиц библиотеки перед тестом."""
    from identity_hub.postgres import metadata

    tables = ", ".join(t.name for t in metadata.sorted_tables)
    async with prepared_db.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield prepared_db
