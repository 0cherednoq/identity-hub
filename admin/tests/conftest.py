"""Shared admin fixtures with PostgreSQL 16 and no external secrets."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from identity_hub.clock import FakeClock
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.postgres import metadata
from identity_hub.postgres.migrate import migrate
from litestar import Litestar
from litestar.testing import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from identity_hub_admin.app import create_admin_app
from tests.factories import ADMIN_TEST_SERVICE, AccountFactory, AdminPrincipalFixture, SeededSpaces
from tests.fakes import RecordingTelegramTransport, RecordingWebhookTransport

TEST_KEYS = {"test-kek": b"\x11" * 32}


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    configured = os.environ.get("IH_TEST_DATABASE_URL")
    if configured:
        yield configured
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    instance = create_async_engine(database_url, pool_size=5, max_overflow=10)
    try:
        await migrate(instance)
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    table_names = ", ".join(table.name for table in metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    yield engine


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def hub(db: AsyncEngine, fake_clock: FakeClock) -> IdentityHub:
    sessions = async_sessionmaker(db, class_=AsyncSession, expire_on_commit=False)
    instance = IdentityHub(
        sessions,
        StaticSecretsProvider(TEST_KEYS, "test-kek"),
        [ADMIN_TEST_SERVICE],
        clock=fake_clock,
        strict_events=True,
        admin_base_url="http://admin.test",
    )
    await instance.start()
    return instance


@pytest.fixture
async def seeded_spaces(hub: IdentityHub) -> SeededSpaces:
    alpha = hub.space("alpha", "Alpha", create=True)
    beta = hub.space("beta", "Beta", create=True)
    alpha_factory = AccountFactory(alpha, "alpha")
    beta_factory = AccountFactory(beta, "beta")
    alpha_ids = tuple([await alpha_factory.create(), await alpha_factory.create()])
    beta_ids = tuple([await beta_factory.create(), await beta_factory.create()])
    return SeededSpaces(alpha, beta, alpha_ids, beta_ids)


@pytest.fixture
def principals() -> dict[str, AdminPrincipalFixture]:
    spaces = frozenset({"alpha", "beta"})
    return {
        "viewer": AdminPrincipalFixture(actor="test:viewer", role="viewer", allowed_spaces=spaces),
        "operator": AdminPrincipalFixture(actor="test:operator", role="operator", allowed_spaces=spaces),
        "admin": AdminPrincipalFixture(actor="test:admin", role="admin", allowed_spaces=spaces),
    }


@pytest.fixture
def request_id() -> str:
    return "test-request-0001"


@pytest.fixture
def http_client(request_id: str) -> Iterator[TestClient[Litestar]]:
    with TestClient(create_admin_app()) as client:
        client.headers.update({"X-Request-ID": request_id})
        yield client


@pytest.fixture
def telegram_transport() -> RecordingTelegramTransport:
    return RecordingTelegramTransport()


@pytest.fixture
def webhook_transport() -> RecordingWebhookTransport:
    return RecordingWebhookTransport()
