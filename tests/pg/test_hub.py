"""P2.1: IdentityHub, пространства, правила владения транзакциями (ТЗ FR-1.3, T-1, T-3)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub._ids import new_id
from identity_hub.errors import NotFound, SpaceMismatch, ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import ServiceRecord, SpaceRecord
from identity_hub.postgres.store import PgTx
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


async def test_start_syncs_service_registry(
    hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with sessionmaker() as s:
        rows = {
            row.name: (row.title, row.url)
            for row in (await s.execute(select(ServiceRecord.name, ServiceRecord.title, ServiceRecord.url)))
        }
    assert rows == {
        "fake_tg": ("Fake Telegram", "https://telegram.org"),
        "fake_exchange": ("Fake Exchange", None),
    }
    async with sessionmaker() as s:
        read_rows = dict((await s.execute(select(hub.read.services.c.name, hub.read.services.c.url))).all())
    assert read_rows["fake_tg"] == "https://telegram.org"
    await hub.start()  # повторный старт — upsert


async def test_service_lookup(hub: IdentityHub) -> None:
    assert hub.service("fake_tg") is FAKE_TG
    assert hub.service(FAKE_TG) is FAKE_TG
    with pytest.raises(NotFound):
        hub.service("nope")


def test_hub_requires_keys_and_storage(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(ValidationFailed):
        IdentityHub(sessionmaker, None)
    with pytest.raises(ValidationFailed):
        IdentityHub(None, None)


async def test_space_created_once_with_custom_id(
    hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    custom = new_id()
    first = await hub.space("alpha", "Alpha", space_id=custom, create=True).ref()
    assert first.id == custom
    again = await IdentityHub(sessionmaker, _keys(), []).space("alpha").ref()
    assert again == first
    with pytest.raises(SpaceMismatch):
        await IdentityHub(sessionmaker, _keys(), []).space("alpha", space_id=new_id()).ref()
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(SpaceRecord))).scalar_one() == 1


def _keys() -> object:
    from identity_hub.crypto import StaticSecretsProvider

    return StaticSecretsProvider(TEST_KEYS, "k1")


async def test_own_transaction_sets_space_config(space: SpaceHandle) -> None:
    async with space._tx() as tx:
        assert isinstance(tx, PgTx)
        setting = (await tx.session.execute(text("SELECT current_setting('ih.space', true)"))).scalar_one()
        assert setting == "tennet"
        assert await tx.now() == await tx.now()


async def _insert_marker(tx: PgTx, slug: str) -> None:
    await tx.execute(SpaceRecord.__table__.insert().values(id=new_id(), slug=slug, title=slug))


async def _exists(sessionmaker: async_sessionmaker[AsyncSession], slug: str) -> bool:
    async with sessionmaker() as s:
        return (await s.execute(select(SpaceRecord.id).where(SpaceRecord.slug == slug))).first() is not None


async def test_user_session_rollback_undoes_library_write(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await space.ref()
    async with sessionmaker() as user, user.begin():
        async with space.using(user)._tx() as tx:
            await _insert_marker(tx, "shared-marker")  # type: ignore[arg-type]
        await user.rollback()
    assert not await _exists(sessionmaker, "shared-marker")

    async with sessionmaker() as user, user.begin(), space.using(user)._tx() as tx:
        await _insert_marker(tx, "committed-marker")  # type: ignore[arg-type]
    assert await _exists(sessionmaker, "committed-marker")


async def test_own_transaction_ignores_user_session(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await space.ref()
    async with sessionmaker() as user:
        await user.begin()
        async with space.using(user)._tx(own=True) as tx:
            await _insert_marker(tx, "own-marker")  # type: ignore[arg-type]
        await user.rollback()
    assert await _exists(sessionmaker, "own-marker")


async def test_library_savepoint_failure_keeps_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await space.ref()
    async with sessionmaker() as user, user.begin():
        with pytest.raises(RuntimeError):
            async with space.using(user)._tx() as tx:
                await _insert_marker(tx, "rolled-back")  # type: ignore[arg-type]
                raise RuntimeError("boom")
        await user.execute(SpaceRecord.__table__.insert().values(id=new_id(), slug="user-row", title="u"))
    assert not await _exists(sessionmaker, "rolled-back")
    assert await _exists(sessionmaker, "user-row")


async def test_session_bound_to_one_space(
    hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a, b = hub.space("a", create=True), hub.space("b", create=True)
    async with sessionmaker() as user, user.begin():
        async with a.using(user)._tx():
            pass
        with pytest.raises(SpaceMismatch):
            async with b.using(user)._tx():
                pass
