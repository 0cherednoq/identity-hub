"""C1.1: `proxies.create_many` — число запросов не зависит от размера пачки."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from identity_hub.hub import SpaceHandle

pytestmark = pytest.mark.pg


async def _count(db: AsyncEngine, action: Any) -> int:
    seen: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        await action()
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)
    return len(seen)


async def test_query_count_does_not_grow_with_batch(space: SpaceHandle, db: AsyncEngine) -> None:
    """C1.1: 1000 прокси — столько же запросов, сколько 10; повтор пачки тоже без запроса на строку."""
    await space.ref()
    small = [f"http://u:p@10.4.{n // 250}.{n % 250}:80" for n in range(10)]
    large = [f"http://u:p@10.5.{n // 250}.{n % 250}:80" for n in range(1000)]

    few = await _count(db, lambda: space.proxies.create_many(small, group="res"))
    many = await _count(db, lambda: space.proxies.create_many(large, group="res"))
    again = await _count(db, lambda: space.proxies.create_many(large, group="res", on_conflict="skip"))

    assert many == few and again <= few
    result = await space.proxies.create_many(
        [*large[:3], "http://u:p@10.6.0.1:80"], group="res", on_conflict="skip"
    )
    assert [item.created for item in result.items] == [False, False, False, True]


async def test_create_many_in_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """C1.1: пачка под `using(session)` видна до фиксации и исчезает при откате приложения."""
    await space.ref()
    async with sessionmaker() as user:
        await user.begin()
        result = await space.using(user).proxies.create_many(
            ["http://10.7.0.1:80", "http://10.7.0.2:80"], group="res"
        )
        assert [p.host for p in result.created] == ["10.7.0.1", "10.7.0.2"]
        await user.rollback()
    assert (await space.proxies.list(group="res")).items == []
