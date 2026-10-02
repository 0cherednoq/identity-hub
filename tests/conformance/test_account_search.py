"""F4.4a: поиск аккаунтов одинаков на обоих хранилищах (R2-D05, R2-D22, R2-P16)."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import TEST_KEYS

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle, StaticSecretsProvider, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.read.search import AccountSearchOptions
from identity_hub.service import Service
from identity_hub.testing import MemoryStore

pytestmark = pytest.mark.conformance


class _Profile(BaseModel):
    level: int | None = None
    vip: bool | None = None
    city: str | None = None


GAME = Service(
    name="game",
    title="Game",
    profile=_Profile,
    filterable_profile=frozenset({"level", "vip", "city"}),
)


@pytest.fixture(params=["memory", "postgres"], ids=["memory", "postgres"])
async def hub(
    request: pytest.FixtureRequest, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[IdentityHub]:
    secrets = StaticSecretsProvider(TEST_KEYS, "k1")
    if request.param == "memory":
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        instance = IdentityHub(secrets=secrets, services=[GAME], store=MemoryStore(clock), clock=clock)
    else:
        instance = IdentityHub(sessionmaker, secrets, [GAME])
    await instance.start()
    yield instance


@pytest.fixture
def space(hub: IdentityHub) -> SpaceHandle:
    return hub.space(f"s-{uuid4().hex[:8]}")


async def _add(space: SpaceHandle, ref: str, **profile: Any) -> UUID:
    identity = await space.identities.create(
        IdentityCreate(service="game", external_ref=ref, profile=profile)
    )
    return identity.id


async def _refs(space: SpaceHandle, sort_by: str, *, desc: bool = False, limit: int = 2) -> list[str]:
    """Все страницы подряд: порядок external_ref при сортировке `sort_by`."""
    options = AccountSearchOptions(sort_by=sort_by, sort_desc=desc)
    refs: list[str] = []
    after: str | None = None
    for _ in range(20):
        page = await space.admin.identity_page("", options=options, limit=limit, after=after)
        refs.extend(item.external_ref for item in page.items)
        after = page.next_cursor
        if after is None:
            return refs
    raise AssertionError("пагинация не завершилась")


def _forged(sort_by: str, kind: str, value: Any) -> str:
    raw = json.dumps([1, sort_by, False, kind, value, str(uuid4())], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def test_second_page_when_sorting_by_numeric_profile_field(space: SpaceHandle) -> None:
    """R2-D05: вторая страница при сортировке по числовому полю профиля не падает."""
    for ref, level in (("a", 30), ("b", 4), ("c", 100), ("d", 25), ("e", None)):
        await _add(space, ref, level=level)

    refs = await _refs(space, "profile:level")

    # поле профиля сортируется как текст: "100" < "25" < "30" < "4"; без значения — в конце
    assert refs == ["c", "d", "a", "b", "e"]
    assert await _refs(space, "profile:level", desc=True) == ["b", "a", "d", "c", "e"]


async def test_boolean_profile_field_sorts_the_same_on_both_backends(space: SpaceHandle) -> None:
    for ref, vip in (("a", True), ("b", False), ("c", True)):
        await _add(space, ref, vip=vip)

    # "false" < "true"; при равных значениях — более новый аккаунт раньше
    assert await _refs(space, "profile:vip", limit=1) == ["b", "c", "a"]


@pytest.mark.parametrize(
    ("sort_by", "kind", "value"),
    [
        ("profile:level", "number", 30),
        ("created_at", "string", "вчера"),
        ("created_at", "number", 5),
        ("error_score", "string", "0.5"),
        ("external_ref", "number", 7),
        ("external_ref", "datetime", "2026-01-01T00:00:00+00:00"),
        ("leased", "string", "1"),
    ],
)
async def test_forged_cursor_is_a_validation_error(
    space: SpaceHandle, sort_by: str, kind: str, value: Any
) -> None:
    """R2-D05: курсор с типом значения не от этой сортировки — `ValidationFailed`, а не ошибка БД."""
    await _add(space, "a", level=1)

    with pytest.raises(ValidationFailed):
        await space.admin.identity_page(
            "",
            options=AccountSearchOptions(sort_by=sort_by, sort_desc=False),
            after=_forged(sort_by, kind, value),
        )


async def test_strings_sort_by_code_points_on_both_backends(space: SpaceHandle) -> None:
    """R2-P16: порядок строк не зависит от локали базы — заглавные раньше строчных, как в Python."""
    refs = ["b", "B", "a", "_x", "Z", "ä", "10", "9"]
    for ref in refs:
        await _add(space, ref, city=ref)

    assert await _refs(space, "external_ref", limit=3) == sorted(refs)
    assert await _refs(space, "profile:city", limit=3) == sorted(refs)
    assert await _refs(space, "external_ref", desc=True, limit=3) == sorted(refs, reverse=True)


@pytest.mark.parametrize("length", [3, 8, 12, 13, 20, 32])
async def test_short_id_matches_hex_suffix_of_any_length(space: SpaceHandle, length: int) -> None:
    """R2-P16: `id:<hex>` — окончание id без дефисов; длиннее 12 символов тоже находится."""
    target = await _add(space, "target")
    await _add(space, "other")

    page = await space.admin.identity_page(f"id:{target.hex[-length:]}")

    assert [item.id for item in page.items] == [target]


async def test_query_limits(space: SpaceHandle) -> None:
    """R2-D22: слишком длинный запрос и слишком много условий отклоняются до обращения к хранилищу."""
    await _add(space, "a")

    with pytest.raises(ValidationFailed):
        await space.admin.identity_page("x" * 2000)
    with pytest.raises(ValidationFailed):
        await space.admin.identity_page(" ".join(f"term{n}" for n in range(100)))
    with pytest.raises(ValidationFailed):
        await space.identities.search("x" * 2000)

    page = await space.admin.identity_page(" ".join(f"word{n}" for n in range(10)))
    assert page.items == ()
