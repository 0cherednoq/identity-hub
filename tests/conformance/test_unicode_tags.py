"""C3.2: теги с буквами Unicode — запись, нормализация NFC, фильтры и пулы."""

from __future__ import annotations

import unicodedata
from uuid import UUID

import pytest

from identity_hub import IdentityCreate, IdentityFilter, Lifecycle, SpaceHandle, ValidationFailed
from identity_hub.domain.data import normalize_tag, validate_tags

pytestmark = pytest.mark.conformance

NFD_CAFE = unicodedata.normalize("NFD", "café")  # «e» и отдельный знак ударения
NFC_CAFE = unicodedata.normalize("NFC", "café")


async def _account(space: SpaceHandle, ref: str, tags: list[str]) -> UUID:
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref=ref,
            tags=tags,
            lifecycle=Lifecycle.READY,
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    return identity.id


def test_tag_rules() -> None:
    """C3.2: буквы и цифры любого письма и знаки `_ . : -`; пробелы и служебные символы — нет."""
    assert validate_tags(["вип", "прогрев:2", "ферма_3", "東京", "naïve-1", "a.b"]) == sorted(
        ["вип", "прогрев:2", "ферма_3", "東京", "naïve-1", "a.b"]
    )
    assert NFD_CAFE != NFC_CAFE and validate_tags([NFD_CAFE, NFC_CAFE]) == [NFC_CAFE]
    assert normalize_tag(NFD_CAFE) == NFC_CAFE
    assert validate_tags(["farm:eu-2", "Farm:EU-2"]) == ["Farm:EU-2", "farm:eu-2"]  # регистр значим
    assert validate_tags(["я" * 64]) == ["я" * 64]
    for bad in ("", "два слова", "таб\tуляция", "строка\n", "я" * 65, "смайл🙂", "a/b", "a,b", "​скрытый"):
        with pytest.raises(ValidationFailed):
            validate_tags([bad])
    with pytest.raises(ValidationFailed):
        validate_tags([1])  # type: ignore[list-item]


async def test_unicode_tags_are_stored_and_found(space: SpaceHandle) -> None:
    """C3.2: кириллический тег записывается, находится фильтром и строкой поиска."""
    vip = await _account(space, "+1", ["вип", NFD_CAFE])
    plain = await _account(space, "+2", ["farm"])

    assert (await space.identities.get(vip)).tags == frozenset({"вип", NFC_CAFE})
    listed = await space.identities.list(IdentityFilter(tags=["вип"]))
    assert [i.id for i in listed.items] == [vip]
    # обе формы одной буквы — один тег: фильтр нормализуется так же, как запись
    for form in (NFD_CAFE, NFC_CAFE):
        assert [i.id for i in (await space.identities.list(IdentityFilter(tags=[form]))).items] == [vip]
        assert [i.id for i in (await space.identities.search(f"tag:{form}")).items] == [vip]
    assert [i.id for i in (await space.identities.search("tag:вип")).items] == [vip]
    assert [i.id for i in (await space.identities.list(IdentityFilter(tags=["farm"]))).items] == [plain]


async def test_unicode_tags_in_commands_and_pools(space: SpaceHandle) -> None:
    """C3.2: команды тегов и отбор пула работают с Unicode и с ненормализованным вводом."""
    vip = await _account(space, "+1", ["вип"])
    await _account(space, "+2", [])

    added = await space.identities.add_tags(vip, [NFD_CAFE, "прогрев:2"], actor="op")
    assert added.tags == frozenset({"вип", NFC_CAFE, "прогрев:2"})
    removed = await space.identities.remove_tags(vip, [NFD_CAFE], actor="op")
    assert removed.tags == frozenset({"вип", "прогрев:2"})
    with pytest.raises(ValidationFailed):
        await space.identities.add_tags(vip, ["два слова"], actor="op")

    pool = space.pools.define("vip", service="fake_tg", tags=["вип"])
    async with pool.lease() as lease:
        assert lease.identity_id == vip
    async with space.pools.define("all", service="fake_tg").lease(tags=["прогрев:2"]) as lease:
        assert lease.identity_id == vip
