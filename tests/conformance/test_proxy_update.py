"""C1.3: смена адреса прокси с сохранением привязок, статуса и счётчиков."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from tests.fixtures.api import register

from identity_hub import AlreadyExists, NotFound, ProxyStatus, SpaceHandle, ValidationFailed
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _events(space: SpaceHandle) -> list[Any]:
    return list((await space.admin.event_page(filters=EventFilters(type="proxy.updated"))).items)


async def _url(space: SpaceHandle, proxy_id: UUID) -> str:
    return await space.proxies.url(proxy_id, actor="op", reason="тест")


async def _bound_count(space: SpaceHandle, proxy_id: UUID) -> int | None:
    return next(item.bound_count for item in (await space.proxies.list()).items if item.id == proxy_id)


async def test_update_changes_address_and_keeps_the_rest(space: SpaceHandle) -> None:
    """C1.3: адрес меняется, логин, пароль, привязки, статус и группа остаются."""
    proxy = await space.proxies.create("http://user:pw-old@10.12.0.1:80", group="res", max_identities=5)
    identity_id = (await register(space, service="fake_tg", external_ref="+1")).identity_id
    await space.proxies.rebind(identity_id, proxy.id, reason="тест", actor="op")
    await space.proxies.override(proxy.id, "quarantined", reason="тест", actor="op")

    updated = await space.proxies.update(
        proxy.id, scheme="socks5", host="NEW.example.test", port=1080, actor="op"
    )

    assert (updated.id, updated.scheme, updated.host, updated.port) == (
        proxy.id,
        "socks5",
        "new.example.test",
        1080,
    )
    assert (updated.status, updated.group, updated.max_identities) == (ProxyStatus.QUARANTINED, "res", 5)
    assert await _url(space, proxy.id) == "socks5://user:pw-old@new.example.test:1080"
    assert await _bound_count(space, proxy.id) == 1
    (event,) = await _events(space)
    assert (event.actor, event.proxy_id) == ("op", proxy.id)
    assert event.data["fields"] == ["scheme", "host", "port"]
    assert (event.data["from_scheme"], event.data["from_host"], event.data["from_port"]) == (
        "http",
        "10.12.0.1",
        80,
    )
    assert (event.data["host"], event.data["port"]) == ("new.example.test", 1080)
    assert "pw-old" not in str(dict(event.data)) and "user" not in str(dict(event.data))
    # прежний адрес свободен, новый занят этим же прокси
    again = await space.proxies.create("http://user:pw-old@10.12.0.1:80", group="res")
    assert again.id != proxy.id
    same, created = await space.proxies.ensure("socks5://user:x@new.example.test:1080", group="res")
    assert (same.id, created) == (proxy.id, False)


async def test_update_changes_credentials_with_address(space: SpaceHandle) -> None:
    """C1.3: логин и пароль меняются вместе с адресом; `None` убирает их, пропуск — оставляет."""
    proxy = await space.proxies.create("http://user:pw-old@10.12.1.1:80", group="res")

    await space.proxies.update(proxy.id, port=81, username="user2", password="pw-new", actor="op")
    assert await _url(space, proxy.id) == "http://user2:pw-new@10.12.1.1:81"

    await space.proxies.update(proxy.id, password="pw-3", actor="op")
    assert await _url(space, proxy.id) == "http://user2:pw-3@10.12.1.1:81"

    await space.proxies.update(proxy.id, username=None, password=None, actor="op")
    assert await _url(space, proxy.id) == "http://10.12.1.1:81"
    fields = [event.data["fields"] for event in reversed(await _events(space))]
    assert fields == [["port", "credentials"], ["credentials"], ["credentials"]]
    assert "pw-new" not in str([dict(event.data) for event in await _events(space)])


async def test_update_rejects_conflicts_and_bad_input(space: SpaceHandle) -> None:
    """C1.3: занятый адрес — `AlreadyExists`; некорректный ввод — `ValidationFailed`; без изменений."""
    proxy = await space.proxies.create("http://user:pw@10.12.2.1:80", group="res")
    other = await space.proxies.create("http://user:pw@10.12.2.2:80", group="res")

    with pytest.raises(AlreadyExists):
        await space.proxies.update(proxy.id, host="10.12.2.2", actor="op")
    with pytest.raises(ValidationFailed):
        await space.proxies.update(proxy.id, scheme="ftp", actor="op")
    with pytest.raises(ValidationFailed):
        await space.proxies.update(proxy.id, port=0, actor="op")
    with pytest.raises(ValidationFailed):
        await space.proxies.update(proxy.id, username=None, actor="op")  # пароль остался бы без логина
    with pytest.raises(ValidationFailed):
        await space.proxies.update(proxy.id, actor="op")  # нечего менять
    with pytest.raises(NotFound):
        await space.proxies.update(uuid4(), host="10.12.2.9", actor="op")

    assert await _url(space, proxy.id) == "http://user:pw@10.12.2.1:80"
    assert await _url(space, other.id) == "http://user:pw@10.12.2.2:80"
    assert await _events(space) == []


async def test_update_with_same_values_is_noop(space: SpaceHandle) -> None:
    """C1.3: те же значения ничего не меняют и события не пишут."""
    proxy = await space.proxies.create("http://user:pw@10.12.3.1:80", group="res")

    same = await space.proxies.update(proxy.id, host="10.12.3.1", port=80, username="user", actor="op")

    assert (same.host, same.port) == ("10.12.3.1", 80)
    assert await _events(space) == []
