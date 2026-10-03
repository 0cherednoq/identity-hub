"""F6.1 (R1-07): пространство создаётся только явно; опечатка в slug — `NotFound`."""

from __future__ import annotations

from uuid import uuid4

import pytest

from identity_hub import AlreadyExists, IdentityCreate, IdentityHub, NotFound


def _slug() -> str:
    return f"sp-{uuid4().hex[:8]}"


pytestmark = pytest.mark.conformance


async def test_typo_in_slug_is_not_found(hub_under_test: IdentityHub) -> None:
    production = await hub_under_test.spaces.create("production", "Production")
    await production.identities.create(IdentityCreate(service="fake_tg", external_ref="+1"))

    typo = hub_under_test.space("prodution")

    with pytest.raises(NotFound):
        await typo.identities.search("")
    assert await hub_under_test.get_space("prodution") is None  # обращение не создало пространство


async def test_spaces_create_ensure_get(hub_under_test: IdentityHub) -> None:
    slug = _slug()
    with pytest.raises(NotFound):
        await hub_under_test.spaces.get(slug)

    created = await hub_under_test.spaces.create(slug, "Первое")
    with pytest.raises(AlreadyExists):
        await hub_under_test.spaces.create(slug, "Второе")
    ensured = await hub_under_test.spaces.ensure(slug, "Не меняется")
    found = await hub_under_test.spaces.get(slug)

    assert (await created.ref()).id == (await ensured.ref()).id == (await found.ref()).id
    assert (await found.ref()).title == "Первое"


async def test_space_with_create_keeps_lazy_creation(hub_under_test: IdentityHub) -> None:
    slug = _slug()
    space = hub_under_test.space(slug, "Лениво", create=True)

    await space.identities.create(IdentityCreate(service="fake_tg", external_ref="+2"))

    assert (await hub_under_test.spaces.get(slug)).slug == slug
