"""N2.3: пересекающиеся bulk-команды берут строки в одном порядке."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from identity_hub import IdentityCreate, SpaceHandle

pytestmark = [pytest.mark.anyio, pytest.mark.pg]


async def test_overlapping_update_many_does_not_deadlock_or_lose_tags(space: SpaceHandle) -> None:
    ids = [UUID(int=701), UUID(int=702), UUID(int=703)]
    for index, identity_id in enumerate(ids):
        await space.identities.create(
            IdentityCreate(id=identity_id, service="fake_tg", external_ref=f"+bulk-race-{index}"),
            actor="test",
        )

    await asyncio.wait_for(
        asyncio.gather(
            space.identities.update_many(ids, add_tags=["left"], actor="left"),
            space.identities.update_many(list(reversed(ids)), add_tags=["right"], actor="right"),
        ),
        timeout=10,
    )

    for identity_id in ids:
        assert (await space.identities.get(identity_id)).tags == {"left", "right"}
