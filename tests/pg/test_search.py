"""P6.1: поиск (ТЗ FR-11.1–11.4, §8.5)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.read.search import COUNT_LIMIT, decode_cursor, encode_cursor

pytestmark = pytest.mark.pg


async def _seed(space: SpaceHandle) -> dict[str, UUID]:
    ids = {}
    for ref, profile, labels, tags, lifecycle in (
        ("+380501", {"first_name": "Olena", "country": "UA"}, {"farm": 3}, ["parser"], "ready"),
        ("+380502", {"first_name": "Petro", "country": "UA"}, {"farm": "4", "vip": True}, [], "ready"),
        ("+48601", {"first_name": "Olga", "country": "PL"}, {"farm": 3}, ["parser"], "provisioning"),
    ):
        r = await space.register(
            service="fake_tg",
            external_ref=ref,
            profile=profile,
            labels=labels,
            tags=tags,
            lifecycle=lifecycle,
        )
        ids[ref] = r.identity_id
    ids["mail"] = (await space.register(service="fake_exchange", external_ref="olena@mail.com")).identity_id
    return ids


async def _refs(space: SpaceHandle, query: str) -> set[str]:
    page = await space.search(query)
    return {row.external_ref for row in page.items}


async def test_filters(space: SpaceHandle) -> None:
    await _seed(space)
    assert await _refs(space, "service:fake_tg") == {"+380501", "+380502", "+48601"}
    assert await _refs(space, "service:fake_tg country:UA") == {"+380501", "+380502"}  # профиль (filterable)
    assert await _refs(space, "farm:3") == {"+380501", "+48601"}  # метка-число по строке
    assert await _refs(space, "farm:4") == {"+380502"}
    assert await _refs(space, "vip:true") == {"+380502"}
    assert await _refs(space, "status:active") == {"+380501", "+380502"}
    assert await _refs(space, "lifecycle:provisioning") == {"+48601", "olena@mail.com"}
    assert await _refs(space, "tag:parser country:PL") == {"+48601"}
    assert await _refs(space, "olena") == {"+380501", "olena@mail.com"}  # подстрока search_text
    assert await _refs(space, "service:fake_tg olena") == {"+380501"}
    assert await _refs(space, "ol") == {
        "+380501",
        "+380502",
        "+48601",
        "olena@mail.com",
    }  # < 3 символов игнорируется
    assert await _refs(space, "380") == {"+380501", "+380502"}
    assert await _refs(space, "100%") == set()  # спецсимволы ILIKE экранируются


async def test_exact_uuid_and_short_id(space: SpaceHandle) -> None:
    ids = await _seed(space)
    target = ids["+48601"]
    assert await _refs(space, str(target)) == {"+48601"}
    assert await _refs(space, f"id:{target.hex[-8:]}") == {"+48601"}


async def test_keyset_pagination_and_count(space: SpaceHandle) -> None:
    for i in range(7):
        await space.register(service="fake_tg", external_ref=f"+1{i:03d}")
    first = await space.search("service:fake_tg", limit=3)
    assert first.total == 7 and first.total_label == "7"
    assert first.next_cursor is not None
    seen = [r.external_ref for r in first.items]
    cursor = first.next_cursor
    while cursor:
        page = await space.search("service:fake_tg", limit=3, after=cursor, count=False)
        assert page.total is None
        seen += [r.external_ref for r in page.items]
        cursor = page.next_cursor
    assert seen == [f"+1{i:03d}" for i in reversed(range(7))]  # новые первыми, без повторов
    with pytest.raises(ValidationFailed):
        await space.search("", after="not-a-cursor")
    created, ident = decode_cursor(encode_cursor(first.items[0].created_at, first.items[0].id))
    assert ident == first.items[0].id and created == first.items[0].created_at


async def test_capped_count(space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    ref = await space.ref()
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO ih_identities (id, space_id, service, external_ref, lifecycle, standing, "
                "auth_state, availability, error_score, fencing_seq, event_seq) "
                "SELECT gen_random_uuid(), :space, 'fake_tg', 'bulk-' || g, 'ready', 'good', 'no_session', "
                "'available', 0, 0, 0 FROM generate_series(1, :n) g"
            ),
            {"space": ref.id, "n": COUNT_LIMIT + 5},
        )
    page = await space.search("", limit=10)
    assert page.total_label == "1000+"


async def test_search_uses_gin_indexes(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(space)
    async with sessionmaker() as s, s.begin():
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        trgm = "\n".join(
            (
                await s.execute(
                    text("EXPLAIN SELECT id FROM ih_identities WHERE search_text ILIKE '%olena%'")
                )
            ).scalars()
        )
        labels = "\n".join(
            (
                await s.execute(
                    text("""EXPLAIN SELECT id FROM ih_identities WHERE labels @> '{"farm": 3}'""")
                )
            ).scalars()
        )
    assert "ix_ih_identities_search" in trgm
    assert "ix_ih_identities_labels" in labels
