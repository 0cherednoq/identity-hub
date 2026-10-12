"""Хаб на MemoryStore и на PostgreSQL для conformance-тестов публичного API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests._backends import selected_backends
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

from identity_hub import IdentityHub, SpaceHandle, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.testing import MemoryStore


@pytest.fixture(params=selected_backends(), ids=str)
def backend_sessionmaker(request: pytest.FixtureRequest) -> async_sessionmaker[AsyncSession] | None:
    """Фабрика сессий выбранного бэкенда; `None` — MemoryStore.

    Фикстура синхронная: PostgreSQL запрашивается лениво только для своего параметра, а вызов
    `getfixturevalue` из асинхронной фикстуры под pytest-asyncio запускал бы цикл внутри цикла.
    """
    if request.param == "memory":
        return None
    sessionmaker: async_sessionmaker[AsyncSession] = request.getfixturevalue("sessionmaker")
    return sessionmaker


@pytest.fixture
async def hub_under_test(
    backend_sessionmaker: async_sessionmaker[AsyncSession] | None,
) -> AsyncIterator[IdentityHub]:
    secrets = StaticSecretsProvider(TEST_KEYS, "k1")
    if backend_sessionmaker is None:
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        hub = IdentityHub(
            secrets=secrets, services=[FAKE_TG, FAKE_EXCHANGE], store=MemoryStore(clock), clock=clock
        )
    else:
        hub = IdentityHub(backend_sessionmaker, secrets, [FAKE_TG, FAKE_EXCHANGE])
    await hub.start()
    yield hub


@pytest.fixture
def space(hub_under_test: IdentityHub) -> SpaceHandle:
    return hub_under_test.space(f"s-{uuid4().hex[:8]}", create=True)
