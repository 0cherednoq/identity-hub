"""F2.4: параллельные изменения одной identity не теряют друг друга (R2-D02)."""

from __future__ import annotations

import asyncio

import pytest
from tests.fixtures.api import register

from identity_hub import SpaceHandle

pytestmark = pytest.mark.conformance

WRITERS = 20


async def test_parallel_add_tags_keep_every_tag(space: SpaceHandle) -> None:
    reg = await register(space, service="fake_tg", external_ref="+1")

    await asyncio.wait_for(
        asyncio.gather(
            *(space.identities.add_tags(reg.identity_id, [f"tag-{n}"], actor=f"w{n}") for n in range(WRITERS))
        ),
        timeout=60,
    )

    identity = await space.identities.get(reg.identity_id)
    assert identity.tags == frozenset(f"tag-{n}" for n in range(WRITERS))


async def test_parallel_label_merges_keep_every_label(space: SpaceHandle) -> None:
    reg = await register(space, service="fake_tg", external_ref="+1", labels={"farm": "eu-1"})

    await asyncio.wait_for(
        asyncio.gather(
            *(
                space.identities.update(reg.identity_id, labels={f"k{n}": n}, actor=f"w{n}")
                for n in range(WRITERS)
            )
        ),
        timeout=60,
    )

    identity = await space.identities.get(reg.identity_id)
    assert dict(identity.labels) == {"farm": "eu-1", **{f"k{n}": n for n in range(WRITERS)}}


async def test_parallel_requirements_keep_every_code(space: SpaceHandle) -> None:
    reg = await register(space, service="fake_tg", external_ref="+1")

    added = await asyncio.wait_for(
        asyncio.gather(
            *(
                space.verifications.requirements.add(reg.identity_id, f"code-{n}", actor="test")
                for n in range(WRITERS)
            )
        ),
        timeout=60,
    )

    assert all(added)
    async with space._tx(own=True) as tx:
        row = await space._identity(tx, reg.identity_id)
    assert {item["code"] for item in row.requirements} == {f"code-{n}" for n in range(WRITERS)}


async def test_mixed_parallel_changes_do_not_overwrite_each_other(space: SpaceHandle) -> None:
    """Теги, метки и заметка меняются разными вызовами одновременно — сохраняется всё."""
    reg = await register(space, service="fake_tg", external_ref="+1")

    await asyncio.wait_for(
        asyncio.gather(
            space.identities.set_note(reg.identity_id, "заметка", actor="a"),
            *(space.identities.add_tags(reg.identity_id, [f"t{n}"], actor="b") for n in range(5)),
            *(space.identities.update(reg.identity_id, labels={f"l{n}": n}, actor="c") for n in range(5)),
        ),
        timeout=60,
    )

    identity = await space.identities.get(reg.identity_id)
    assert identity.note == "заметка"
    assert identity.tags == frozenset(f"t{n}" for n in range(5))
    assert dict(identity.labels) == {f"l{n}": n for n in range(5)}
