"""F4.5: PgBouncer — LISTEN с проверкой доставки и рабочий рецепт подключения (R2-D04, R2-D21).

Тесты рецепта и обнаружения transaction pooling идут, когда `IH_TEST_PGBOUNCER=1` и
`IH_TEST_DATABASE_URL` указывает на PgBouncer в режиме транзакций (job `pgbouncer` в CI).
"""

from __future__ import annotations

import asyncio
import logging
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle
from identity_hub.postgres.store import NOTIFY_CHANNEL, PgStore

pytestmark = pytest.mark.pg

THROUGH_PGBOUNCER = os.environ.get("IH_TEST_PGBOUNCER") == "1"
direct_only = pytest.mark.skipif(THROUGH_PGBOUNCER, reason="нужно прямое подключение к PostgreSQL")
pgbouncer_only = pytest.mark.skipif(not THROUGH_PGBOUNCER, reason="нужен PgBouncer в режиме транзакций")


@direct_only
async def test_listen_delivers_notifications_on_direct_connection(hub: IdentityHub, db: AsyncEngine) -> None:
    async with hub._store.listen(NOTIFY_CHANNEL) as signal:
        assert signal is not None
        async with db.begin() as conn:
            await conn.execute(text("SELECT pg_notify(:channel, 'x')"), {"channel": NOTIFY_CHANNEL})
        await asyncio.wait_for(signal.wait(), timeout=10)


@direct_only
async def test_listen_without_delivery_polls_and_releases_connection(
    hub: IdentityHub, db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R2-D21: уведомления не доходят — режим опроса, сообщение в логе, соединение не занято."""

    async def never(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(PgStore, "_notifications_arrive", never)
    with caplog.at_level(logging.INFO, logger="identity_hub"):
        async with hub._store.listen(NOTIFY_CHANNEL) as signal:
            assert signal is None
            assert db.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]

    assert "опрос" in caplog.text


@pgbouncer_only
async def test_listen_detects_transaction_pooling(hub: IdentityHub, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="identity_hub"):
        async with hub._store.listen(NOTIFY_CHANNEL) as signal:
            assert signal is None
    assert "опрос" in caplog.text


@pgbouncer_only
async def test_documented_recipe_survives_concurrent_operations(space: SpaceHandle) -> None:
    """R2-D04: по рецепту из docs/integrations.md 60 операций через PgBouncer проходят без ошибок."""

    async def one(n: int) -> None:
        await space.identities.ensure(IdentityCreate(service="fake_tg", external_ref=f"pgb-{n % 5}"))
        await space.identities.search("pgb")

    for _ in range(3):
        results = await asyncio.gather(*(one(n) for n in range(20)), return_exceptions=True)
        assert [result for result in results if isinstance(result, BaseException)] == []
