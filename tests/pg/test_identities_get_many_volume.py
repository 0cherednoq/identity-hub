"""C3.5: `identities.get_many` — один запрос на пачку."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityCreate
from identity_hub.hub import SpaceHandle

pytestmark = pytest.mark.pg


async def test_query_count_does_not_grow_with_batch(space: SpaceHandle, db: AsyncEngine) -> None:
    """C3.5: 200 аккаунтов читаются тем же числом запросов, что и 2."""
    created = await space.identities.create_many(
        [IdentityCreate(service="fake_tg", external_ref=f"+{n}") for n in range(200)], actor="test"
    )
    ids = [identity.id for identity in created.created]
    seen: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        seen.append(statement)

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        few = await space.identities.get_many(ids[:2])
        small = len(seen)
        many = await space.identities.get_many(ids)
        large = len(seen) - small
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert (len(few), len(many)) == (2, 200) and large == small
