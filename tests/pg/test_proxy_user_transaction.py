"""C0.1: команды прокси под `space.using(session)` читают результат в транзакции записи."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import EventRecord, ProxyRecord

pytestmark = pytest.mark.pg


async def _count(sessionmaker: async_sessionmaker[AsyncSession], model: type) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_proxy_commands_see_uncommitted_rows(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """C0.1: до фиксации транзакции приложения команда возвращает свою строку, а не `NotFound`."""
    await space.ref()
    async with sessionmaker() as user:
        await user.begin()
        proxies = space.using(user).proxies

        created = await proxies.create("socks5://u:p@10.0.0.1:1080", group="res")
        assert (created.host, created.group) == ("10.0.0.1", "res")

        ensured, is_new = await proxies.ensure("socks5://10.0.0.2:1080", group="res")
        assert is_new and ensured.host == "10.0.0.2"
        same, is_new = await proxies.ensure("socks5://10.0.0.2:1080", group="res")
        assert not is_new and same.id == ensured.id

        many = await proxies.create_many(["socks5://10.0.0.3:1080", "socks5://10.0.0.4:1080"], group="res")
        assert [p.host for p in many.created] == ["10.0.0.3", "10.0.0.4"]

        moved = await proxies.move(created.id, group="dc", actor="op")
        assert moved.group == "dc"

        replaced = await proxies.replace_credentials(created.id, username="u2", password="p2", actor="op")
        assert replaced.id == created.id

        await user.rollback()

    assert await _count(sessionmaker, ProxyRecord) == 0
    assert await _count(sessionmaker, EventRecord) == 0


async def test_proxy_commands_commit_with_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """C0.1: фиксация транзакции приложения сохраняет прокси; без `using` поведение прежнее."""
    await space.ref()
    async with sessionmaker() as user, user.begin():
        created = await space.using(user).proxies.create("socks5://10.0.1.1:1080", group="res")
    assert (await space.proxies.get(created.id)).host == "10.0.1.1"
    plain = await space.proxies.create("socks5://10.0.1.2:1080", group="res")
    assert (await space.proxies.move(plain.id, group="dc", actor="op")).group == "dc"


async def test_batch_commands_roll_back_with_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """C1.2: пакетные удаление и смена статуса идут в транзакции приложения."""
    created = await space.proxies.create_many(
        ["socks5://10.0.2.1:1080", "socks5://10.0.2.2:1080", "socks5://10.0.2.3:1080"], group="res"
    )
    first, second, third = (item.id for item in created.items)
    events = await _count(sessionmaker, EventRecord)

    async with sessionmaker() as user:
        await user.begin()
        proxies = space.using(user).proxies
        overridden = await proxies.override_many({first: "dead"}, reason="тест", actor="op")
        deleted = await proxies.delete_many([second, third], actor="op")
        assert overridden.applied == [first] and sorted(deleted.applied) == sorted([second, third])
        await user.rollback()

    assert await _count(sessionmaker, ProxyRecord) == 3
    assert await _count(sessionmaker, EventRecord) == events
    assert (await space.proxies.get(first)).status == "alive"

    async with sessionmaker() as user, user.begin():
        await space.using(user).proxies.delete_many([second, third], actor="op")
    assert await _count(sessionmaker, ProxyRecord) == 1
