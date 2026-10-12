"""C1.1: `proxies.create_many` со структурными записями и результатом в порядке входа."""

from __future__ import annotations

from uuid import uuid4

import pytest

from identity_hub import AlreadyExists, ProxyCreate, ProxyCreated, SpaceHandle, ValidationFailed

pytestmark = pytest.mark.conformance


async def test_items_map_input_to_ids_in_order(space: SpaceHandle) -> None:
    """C1.1: «вход → id» верно и для двух прокси на одном host:port с разными логинами."""
    own_id = uuid4()
    existing = await space.proxies.create("http://u0@gate.example.test:8000", group="res")
    items = [
        ProxyCreate("http", "gate.example.test", 8000, username="u1", password="p1", id=own_id),
        "http://u2:p2@gate.example.test:8000",
        ProxyCreate("http", "gate.example.test", 8000, username="u0"),  # уже есть
        ProxyCreate.from_url("socks5://10.3.0.1:1080", max_identities=7),
        ProxyCreate("http", "gate.example.test", 8000, username="u1", password="other"),  # повтор в пачке
    ]

    result = await space.proxies.create_many(items, group="res", on_conflict="skip")

    assert [type(item) for item in result.items] == [ProxyCreated] * 5
    assert [item.index for item in result.items] == [0, 1, 2, 3, 4]
    assert [item.created for item in result.items] == [True, True, False, True, False]
    ids = [item.id for item in result.items]
    assert ids[0] == own_id and ids[2] == existing.id and ids[4] == own_id
    assert len({ids[0], ids[1], ids[2], ids[3]}) == 4
    assert [p.id for p in result.created] == [ids[0], ids[1], ids[3]]
    assert result.skipped == [existing.id, own_id]
    by_id = {p.id: p for p in result.created}
    assert by_id[ids[3]].max_identities == 7 and by_id[ids[0]].max_identities == 3
    assert "u1:p1@" in await space.proxies.url(ids[0], actor="test", reason="проверка")
    assert "u2:p2@" in await space.proxies.url(ids[1], actor="test", reason="проверка")


async def test_duplicate_in_batch_fails_by_default(space: SpaceHandle) -> None:
    """C1.1: при `on_conflict="fail"` повтор адреса в пачке откатывает всё."""
    with pytest.raises(AlreadyExists):
        await space.proxies.create_many(
            ["http://10.3.1.1:80", "http://10.3.1.2:80", "http://10.3.1.1:80"], group="g"
        )

    assert (await space.proxies.list(group="g")).items == []


async def test_proxy_create_validates_and_hides_password() -> None:
    """C1.1: пароль не виден в `repr`; запись без хоста или порта отклоняется."""
    item = ProxyCreate("socks5", "10.3.2.1", 1080, username="user", password="pw-secret")

    assert "pw-secret" not in repr(item) and "pw-secret" not in str(item)
    assert "pw-secret" not in repr(ProxyCreate.from_url("socks5://user:pw-secret@10.3.2.1:1080"))
    with pytest.raises(ValidationFailed):
        ProxyCreate.from_url("ftp://10.3.2.1:21")


async def test_invalid_item_rejects_whole_batch(space: SpaceHandle) -> None:
    """C1.1: некорректная запись отклоняет пачку до вставки."""
    with pytest.raises(ValidationFailed):
        await space.proxies.create_many(
            ["http://10.3.3.1:80", ProxyCreate("http", "10.3.3.2", 80, password="без логина")], group="g"
        )
    with pytest.raises(ValidationFailed):
        await space.proxies.create_many([ProxyCreate("http", "10.3.3.3", 80, max_identities=0)], group="g")

    assert (await space.proxies.list(group="g")).items == []
    empty = await space.proxies.create_many([], group="g")
    assert empty.items == [] and empty.created == [] and empty.skipped == []
