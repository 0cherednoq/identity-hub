"""C3.3: список тегов пространства с числом аккаунтов."""

from __future__ import annotations

from uuid import UUID

import pytest

from identity_hub import (
    IdentityCreate,
    IdentityFilter,
    IdentityHub,
    NotFound,
    SpaceHandle,
    TagCount,
    ValidationFailed,
)

pytestmark = pytest.mark.conformance


async def _account(space: SpaceHandle, ref: str, tags: list[str], service: str = "fake_tg") -> UUID:
    spec = IdentityCreate(service=service, external_ref=ref, tags=tags)
    return (await space.identities.create(spec, actor="test")).id


async def test_tags_are_counted_per_space_and_service(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    """C3.3: счётчики совпадают с `identities.count`; чужое пространство и удалённые не учитываются."""
    await _account(space, "+1", ["вип", "farm:eu"])
    await _account(space, "+2", ["вип"])
    gone = await _account(space, "+3", ["вип", "уйдёт"])
    await _account(space, "ex-1", ["вип", "биржа"], service="fake_exchange")
    await _account(space, "+4", [])
    other = hub_under_test.space(f"{space.slug}-other", create=True)
    await _account(other, "+9", ["вип", "чужой"])
    await space.identities.delete(gone, actor="test")

    counts = await space.identities.tags()

    assert counts == [TagCount("вип", 3), TagCount("farm:eu", 1), TagCount("биржа", 1)]
    for item in counts:
        assert item.count == await space.identities.count(IdentityFilter(tags=[item.tag]))
    assert await space.identities.tags(service="fake_tg") == [TagCount("вип", 2), TagCount("farm:eu", 1)]
    assert await space.identities.tags(service="fake_exchange") == [TagCount("биржа", 1), TagCount("вип", 1)]
    assert await space.identities.tags(limit=1) == [TagCount("вип", 3)]
    assert await other.identities.tags() == [TagCount("вип", 1), TagCount("чужой", 1)]


async def test_tags_validate_arguments(space: SpaceHandle) -> None:
    """C3.3: пустое пространство — пустой список; некорректный предел и неизвестный сервис отклоняются."""
    assert await space.identities.tags() == []
    for limit in (0, 1001):
        with pytest.raises(ValidationFailed):
            await space.identities.tags(limit=limit)
    with pytest.raises(NotFound):
        await space.identities.tags(service="нет такого")
