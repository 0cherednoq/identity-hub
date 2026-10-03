"""Хаб на MemoryStore и на PostgreSQL для conformance-тестов публичного API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG

from identity_hub import IdentityHub, SpaceHandle, StaticSecretsProvider
from identity_hub.clock import FakeClock
from identity_hub.testing import MemoryStore


@pytest.fixture(params=["memory", "postgres"], ids=["memory", "postgres"])
async def hub_under_test(
    request: pytest.FixtureRequest, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[IdentityHub]:
    secrets = StaticSecretsProvider(TEST_KEYS, "k1")
    if request.param == "memory":
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        hub = IdentityHub(
            secrets=secrets, services=[FAKE_TG, FAKE_EXCHANGE], store=MemoryStore(clock), clock=clock
        )
    else:
        hub = IdentityHub(sessionmaker, secrets, [FAKE_TG, FAKE_EXCHANGE])
    await hub.start()
    yield hub


@pytest.fixture
def space(hub_under_test: IdentityHub) -> SpaceHandle:
    return hub_under_test.space(f"s-{uuid4().hex[:8]}", create=True)
