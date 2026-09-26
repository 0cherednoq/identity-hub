"""Общие фикстуры: PostgreSQL 16 (testcontainers или IH_TEST_DATABASE_URL)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
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
