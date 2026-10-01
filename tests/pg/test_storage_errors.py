"""F1.3 (R2-S03): ошибки БД выходят из хранилища без SQL, параметров и `token` аренды."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from identity_hub import DatabaseBusy, IdentityHub, IdentityHubError, StaticSecretsProvider, StorageError
from identity_hub.lease import Lease
from identity_hub.postgres.models import ServiceRecord
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG
from tests.pg.helpers import make_identity

pytestmark = pytest.mark.pg


@pytest.fixture
async def impatient_hub(db: AsyncEngine, database_url: str) -> AsyncIterator[IdentityHub]:
    """Хаб, чьи соединения работают с `statement_timeout` — типичная защита продакшена."""
    engine = create_async_engine(database_url, connect_args={"server_settings": {"statement_timeout": "300"}})
    hub = IdentityHub(
        async_sessionmaker(engine, expire_on_commit=False),
        StaticSecretsProvider(TEST_KEYS, "k1"),
        [FAKE_TG, FAKE_EXCHANGE],
    )
    await hub.start()
    try:
        yield hub
    finally:
        await engine.dispose()


async def _lease(hub: IdentityHub) -> Lease:
    space = hub.space("tennet", "Tennet")
    await make_identity(space)
    return await space.pools.define("p", service="fake_tg", holder="w").acquire()


def _assert_clean(text_: str, token: str) -> None:
    assert token not in text_
    assert "[parameters" not in text_ and "[SQL" not in text_
    assert "UPDATE" not in text_ and "ih_leases" not in text_


async def test_statement_timeout_on_locked_lease_row_hides_token(
    impatient_hub: IdentityHub,
    sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    lease = await _lease(impatient_hub)
    token = str(lease._token)
    caplog.set_level(logging.DEBUG, logger="identity_hub")
    async with sessionmaker() as locker, locker.begin():
        await locker.execute(text("SELECT 1 FROM ih_leases FOR UPDATE"))

        with pytest.raises(DatabaseBusy) as exc:
            await lease._extend()
        await lease.release()  # освобождение не проходит и только пишет предупреждение

    error = exc.value
    _assert_clean(str(error), token)
    _assert_clean(repr(error), token)
    _assert_clean(str(error.context), token)
    assert error.__cause__ is None and error.__context__ is None  # исходное исключение не сохранено
    assert error.context == {"sqlstate": "57014"}
    assert "не удалось освободить аренду" in caplog.text
    _assert_clean(caplog.text, token)
    for record in caplog.records:
        assert record.exc_info is None  # traceback с параметрами запроса в лог не уходит


async def test_constraint_violation_is_library_error_without_parameters(impatient_hub: IdentityHub) -> None:
    space = impatient_hub.space("tennet", "Tennet")
    with pytest.raises(StorageError) as exc:
        async with space._tx(own=True) as tx:
            await tx.execute(insert(ServiceRecord).values(name="fake_tg", title="SECRET-TITLE"))

    error = exc.value
    assert isinstance(error, IdentityHubError) and not isinstance(error, DatabaseBusy)
    assert error.code == "storage_error"
    assert error.context == {"sqlstate": "23505"}
    assert "SECRET-TITLE" not in str(error) and "[parameters" not in str(error)
    assert error.__cause__ is None and error.__context__ is None


async def test_error_inside_user_session_is_translated_too(
    impatient_hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    space = impatient_hub.space("tennet", "Tennet")
    async with sessionmaker() as session, session.begin():
        with pytest.raises(StorageError) as exc:
            async with space.using(session)._tx() as tx:
                await tx.execute(insert(ServiceRecord).values(name="fake_tg", title="SECRET-TITLE"))
        assert "SECRET-TITLE" not in str(exc.value)
        await session.rollback()
