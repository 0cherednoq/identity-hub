"""F1.6a: секреты вне аренды, аудит раскрытия и repr (R2-S05, R2-S10, R2-S14, R2-F19)."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    IdentityCreate,
    IdentityHub,
    ImportRecord,
    LeaseLost,
    ProxyPolicy,
    SecretRef,
    SecretValue,
    SpaceHandle,
    StaleFencing,
    ValidationFailed,
)
from identity_hub.lease import Lease
from identity_hub.pool import Pool
from identity_hub.proxies import parse_proxy_url
from identity_hub.read.events import EventFilters
from identity_hub.store.base import LeaseRow

pytestmark = pytest.mark.conformance

PROXY_URL = "socks5://puser:PROXY-PW@10.1.1.1:1080"


async def _pool(space: SpaceHandle) -> Pool:
    await register(
        space,
        service="fake_tg",
        external_ref="+1",
        lifecycle="ready",
        credentials={"phone": "+1", "password": "PW-1"},
        session={"auth_key": "AK-1"},
    )
    return space.pools.define("p", service="fake_tg", holder="w")


async def _assert_no_secrets(lease: Lease) -> None:
    with pytest.raises(LeaseLost):
        await lease.credentials()
    with pytest.raises(LeaseLost):
        await lease.session()
    # запись устаревшей аренды по-прежнему отвергает fence (G-1.4)
    with pytest.raises((LeaseLost, StaleFencing)):
        await lease.save_session({"auth_key": "AK-2"})


# ---------------------------------------------------------------- R2-S05


async def test_released_lease_does_not_decrypt_secrets(space: SpaceHandle) -> None:
    """R2-S05: после `release()` и после выхода из `async with` объект аренды секреты не отдаёт."""
    pool = await _pool(space)
    lease = await pool.acquire()
    assert (await lease.credentials()).password.get_secret_value() == "PW-1"  # type: ignore[union-attr]
    await lease.release()
    await _assert_no_secrets(lease)
    with pytest.raises(LeaseLost):
        await lease.save_session({"auth_key": "AK-2"})

    async with pool.lease() as scoped:
        assert await scoped.session() is not None
    await _assert_no_secrets(scoped)


async def test_revoked_lease_does_not_decrypt_secrets_before_heartbeat(space: SpaceHandle) -> None:
    """R2-S05: отзыв виден при чтении секрета, а не только на следующем heartbeat."""
    pool = await _pool(space)
    lease = await pool.acquire()
    assert await space.pools.revoke(lease.identity_id, reason="admin", actor="admin") is True
    await _assert_no_secrets(lease)


async def test_stale_lease_does_not_decrypt_secrets_of_account_taken_by_another_worker(
    space: SpaceHandle,
) -> None:
    pool = await _pool(space)
    stale = await pool.acquire()
    await space.pools.revoke(stale.identity_id, reason="admin", actor="admin")
    current = await pool.acquire()
    assert current.fencing > stale.fencing
    await _assert_no_secrets(stale)
    assert await current.credentials() is not None
    await current.release()


async def test_lease_proxy_is_secret_value(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """R2-S14: `Lease.proxy` — не строка с паролем; значение даёт только `reveal()`."""
    service = replace(FAKE_TG, name="proxied", proxy_policy=ProxyPolicy(group="g"))
    hub_under_test.register_service(service)
    await hub_under_test.start()
    await space.proxies.create(PROXY_URL, group="g")
    await register(space, service="proxied", external_ref="+2", lifecycle="ready", session={"auth_key": "k"})
    async with space.pools.define("px", service="proxied", holder="w").lease() as lease:
        proxy = lease.proxy
        assert isinstance(proxy, SecretValue)
        assert "PROXY-PW" not in repr(proxy) and "PROXY-PW" not in f"{proxy}"
        assert proxy.reveal() == PROXY_URL
    with pytest.raises(LeaseLost):
        _ = lease.proxy


# ---------------------------------------------------------------- R2-S10


async def _events(space: SpaceHandle, type_: str) -> list[Any]:
    page = await space.admin.event_page(filters=EventFilters(type=type_))
    return list(page.items)


async def test_proxy_url_is_revealed_with_audit_event(space: SpaceHandle) -> None:
    """R2-S10: URL прокси с паролем выдаётся только с автором и причиной, раскрытие — в журнале."""
    proxy = await space.proxies.create(PROXY_URL, group="g")

    assert await space.proxies.url(proxy.id, actor="ops:ivan", reason="проверка канала") == PROXY_URL

    (event,) = await _events(space, "proxy.secret.revealed")
    assert (event.actor, event.proxy_id, event.severity) == ("ops:ivan", proxy.id, "warning")
    assert dict(event.data) == {"reason": "проверка канала"}


@pytest.mark.parametrize(("actor", "reason"), [("", "r"), ("  ", "r"), ("ops", ""), ("ops", " \t")])
async def test_proxy_url_requires_actor_and_reason(space: SpaceHandle, actor: str, reason: str) -> None:
    proxy = await space.proxies.create(PROXY_URL, group="g")
    with pytest.raises(ValidationFailed):
        await space.proxies.url(proxy.id, actor=actor, reason=reason)
    with pytest.raises(TypeError):
        await space.proxies.url(proxy.id)  # type: ignore[call-arg]
    assert await _events(space, "proxy.secret.revealed") == []


@pytest.mark.parametrize(("actor", "reason"), [("", "r"), (" ", "r"), ("admin", ""), ("admin", "   ")])
async def test_secret_reveal_requires_actor_and_reason(space: SpaceHandle, actor: str, reason: str) -> None:
    """R2-S10: `reason=None` и `actor=' '` больше не дают раскрыть секрет."""
    identity = await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref="+3", credentials={"phone": "+3", "password": "PW"})
    )
    with pytest.raises(ValidationFailed):
        await space.secrets.reveal(identity.id, SecretRef.CREDENTIALS, actor=actor, reason=reason)
    with pytest.raises(TypeError):
        await space.secrets.reveal(identity.id, SecretRef.CREDENTIALS, actor="admin")  # type: ignore[call-arg]
    with pytest.raises(ValidationFailed):
        await space.secrets.reveal(identity.id, SecretRef.CREDENTIALS, actor="admin", reason=None)  # type: ignore[arg-type]
    assert await _events(space, "identity.secret.revealed") == []

    revealed = await space.secrets.reveal(
        identity.id, SecretRef.CREDENTIALS, actor=" admin ", reason=" аудит "
    )
    assert revealed["password"] == "PW"
    (event,) = await _events(space, "identity.secret.revealed")
    assert (event.actor, event.data["reason"]) == ("admin", "аудит")


# ---------------------------------------------------------------- R2-S14, R2-F19


def test_repr_does_not_print_secrets() -> None:
    record = ImportRecord(
        external_ref="+7",
        credentials={"password": "CRED-PW"},
        sessions={"default": {"auth_key": "SESSION-KEY"}},
        profile={"first_name": "PROFILE-NAME"},
        proxy=PROXY_URL,
    )
    token = uuid4()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row = LeaseRow(uuid4(), token, uuid4(), 1, "p", "w", now, now)
    create = IdentityCreate(
        service="fake_tg",
        external_ref="+7",
        credentials={"password": "CRED-PW"},
        session={"auth_key": "SESSION-KEY"},
        sessions={"web": {"auth_key": "SESSION-KEY"}},
        profile={"first_name": "PROFILE-NAME"},
    )
    parsed = parse_proxy_url(PROXY_URL)

    text = " ".join(map(repr, (record, row, create, parsed)))
    for leak in ("CRED-PW", "SESSION-KEY", "PROFILE-NAME", "PROXY-PW", str(token)):
        assert leak not in text
    assert "external_ref='+7'" in repr(record) and "host='10.1.1.1'" in repr(parsed)
    assert parsed.password == "PROXY-PW" and row.token == token  # значения на месте, скрыт только repr
    hidden = {f.name for f in dataclasses.fields(IdentityCreate) if not f.repr}
    assert {"credentials", "session", "sessions", "profile"} <= hidden
