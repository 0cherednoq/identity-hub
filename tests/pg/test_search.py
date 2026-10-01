"""P6.1: поиск (ТЗ FR-11.1–11.4, §8.5)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.read.search import COUNT_LIMIT, AccountSearchOptions, decode_cursor, encode_cursor
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


async def _seed(space: SpaceHandle) -> dict[str, UUID]:
    ids = {}
    for ref, profile, labels, tags, lifecycle in (
        ("+380501", {"first_name": "Olena", "country": "UA"}, {"farm": 3}, ["parser"], "ready"),
        ("+380502", {"first_name": "Petro", "country": "UA"}, {"farm": "4", "vip": True}, [], "ready"),
        ("+48601", {"first_name": "Olga", "country": "PL"}, {"farm": 3}, ["parser"], "provisioning"),
    ):
        r = await register(
            space,
            service="fake_tg",
            external_ref=ref,
            profile=profile,
            labels=labels,
            tags=tags,
            lifecycle=lifecycle,
        )
        ids[ref] = r.identity_id
    ids["mail"] = (await register(space, service="fake_exchange", external_ref="olena@mail.com")).identity_id
    return ids


async def _refs(space: SpaceHandle, query: str) -> set[str]:
    page = await space.identities.search(query)
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
        await register(space, service="fake_tg", external_ref=f"+1{i:03d}")
    first = await space.identities.search("service:fake_tg", limit=3, count=True)
    assert first.total == 7
    assert first.next_cursor is not None
    seen = [r.external_ref for r in first.items]
    cursor: str | None = first.next_cursor
    while cursor:
        page = await space.identities.search("service:fake_tg", limit=3, after=cursor, count=False)
        assert page.total is None
        seen += [r.external_ref for r in page.items]
        cursor = page.next_cursor
    assert seen == [f"+1{i:03d}" for i in reversed(range(7))]  # новые первыми, без повторов
    with pytest.raises(ValidationFailed):
        await space.identities.search("", after="not-a-cursor")
    created, ident = decode_cursor(encode_cursor(first.items[0].created_at, first.items[0].id))
    assert ident == first.items[0].id and created == first.items[0].created_at


async def test_account_search_equal_timestamps_and_structured_filter(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _seed(space)
    await space.verifications.requirements.add(ids["+380501"], "captcha")
    ref = await space.ref()
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE ih_identities SET created_at = '2026-01-01T00:00:00+00:00' WHERE space_id = :space"),
            {"space": ref.id},
        )

    filtered = await space.admin.identity_page(options=AccountSearchOptions(requirement="captcha"))
    assert [item.external_ref for item in filtered.items] == ["+380501"]
    assert filtered.items[0].requirements == ("captcha",)

    seen: list[UUID] = []
    cursor: str | None = None
    while True:
        page = await space.admin.identity_page(limit=2, after=cursor, count=cursor is None)
        seen.extend(item.id for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(seen) == len(set(seen)) == 4

    sorted_refs: list[str] = []
    cursor = None
    while True:
        page = await space.admin.identity_page(
            options=AccountSearchOptions(sort_by="external_ref", sort_desc=False),
            limit=2,
            after=cursor,
        )
        sorted_refs.extend(item.external_ref for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert sorted_refs == ["+380501", "+380502", "+48601", "olena@mail.com"]

    snapshot = await space.admin.selection_snapshot(
        "service:fake_tg", options=AccountSearchOptions(sort_by="external_ref"), limit=2
    )
    assert [entry.external_ref for entry in snapshot.entries] == ["+48601", "+380502"]
    assert all(entry.revision >= 1 for entry in snapshot.entries)
    assert snapshot.truncated

    for sort_by in (
        "eligible",
        "verification_tier",
        "last_leased_at",
        "synced_at",
        "error_score",
        "profile:country",
    ):
        sorted_ids: list[UUID] = []
        cursor = None
        while True:
            page = await space.admin.identity_page(
                options=AccountSearchOptions(sort_by=sort_by),
                limit=2,
                after=cursor,
            )
            sorted_ids.extend(item.id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert len(sorted_ids) == len(set(sorted_ids)) == 4, sort_by


async def test_capped_count(space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    ref = await space.ref()
    async with sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "INSERT INTO ih_identities (id, space_id, service, external_ref, normalized_ref, lifecycle, "
                "standing, auth_state, availability, error_score, fencing_seq, event_seq) "
                "SELECT gen_random_uuid(), :space, 'fake_tg', 'bulk-' || g, 'bulk-' || g, 'ready', 'good', "
                "'no_session', "
                "'available', 0, 0, 0 FROM generate_series(1, :n) g"
            ),
            {"space": ref.id, "n": COUNT_LIMIT + 5},
        )
    page = await space.identities.search("", limit=10, count=True)
    assert page.total is not None and page.total > 1000  # «1000+»


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
