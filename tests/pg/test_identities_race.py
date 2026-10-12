"""IMP-23 (I8.3): параллельные пересекающиеся `create_many` без deadlock и дублей."""

from __future__ import annotations

import asyncio

import pytest

from identity_hub import AlreadyExists, IdentityCreate, SpaceHandle

pytestmark = [pytest.mark.pg]


def _specs(refs: list[str]) -> list[IdentityCreate]:
    return [IdentityCreate(service="fake_tg", external_ref=ref) for ref in refs]


async def test_overlapping_create_many_skip_without_deadlock(space: SpaceHandle) -> None:
    refs = [f"+7{n:03d}" for n in range(60)]
    forward, backward = _specs(refs), _specs(list(reversed(refs)))

    results = await asyncio.wait_for(
        asyncio.gather(
            space.identities.create_many(forward, on_conflict="skip", actor="test"),
            space.identities.create_many(backward, on_conflict="skip", actor="test"),
        ),
        timeout=30,
    )

    created = [i.external_ref for result in results for i in result.created]
    skipped = [r.external_ref for result in results for r in result.skipped]
    assert sorted(created) == refs
    assert sorted(created + skipped) == sorted(refs * 2)
    assert await space.identities.count() == len(refs)


async def test_overlapping_create_many_fail_keeps_one_batch(space: SpaceHandle) -> None:
    left = _specs(["+8001", "+8002", "+8003"])
    right = _specs(["+8003", "+8004"])

    outcomes = await asyncio.wait_for(
        asyncio.gather(
            space.identities.create_many(left, actor="test"),
            space.identities.create_many(right, actor="test"),
            return_exceptions=True,
        ),
        timeout=30,
    )

    errors = [o for o in outcomes if isinstance(o, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], AlreadyExists)
    # общий ref +8003 — победившая пачка создана целиком, проигравшая не оставила ничего
    assert await space.identities.count() in {len(left), len(right)}
