"""IMP-09 (I3.2a): credentials по виду под арендой, в контексте входа и раскрытие с аудитом (D-216)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, SecretRef, SpaceHandle
from identity_hub.clock import FakeClock
from identity_hub.errors import LeaseLost
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


class ApiKey(BaseModel):
    secret: SecretStr


async def _setup(hub: IdentityHub, space: SpaceHandle) -> UUID:
    hub.register_service(replace(FAKE_TG, name="fake_keys", credential_kinds={"api_key": ApiKey}))
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(
            service="fake_keys",
            external_ref="+1",
            lifecycle="ready",
            credentials={"phone": "+1", "password": "p"},
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    now = hub.clock.now()
    for name, expires in (
        ("trading", None),
        ("readonly", None),
        ("old", now + timedelta(seconds=1)),
        ("off", None),
    ):
        await space.secrets.put_credential(
            created.id, "api_key", {"secret": f"s-{name}"}, name=name, expires_at=expires, actor="ops"
        )
    await space.secrets.revoke_credential(created.id, "api_key", "readonly", actor="ops")
    await space.secrets.disable_credential(created.id, "api_key", "off", actor="ops")
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=2))
    else:
        await asyncio.sleep(1.2)
    return created.id


async def test_lease_reads_credential_by_kind(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _setup(hub_under_test, space)
    lease = await space.pools.define("work", service="fake_keys", holder="w").acquire()

    trading = await lease.credential("api_key", "trading")
    assert isinstance(trading, ApiKey) and trading.secret.get_secret_value() == "s-trading"
    for name in ("readonly", "off", "old", "missing"):  # отозванная, выключенная, истёкшая, нет записи
        assert await lease.credential("api_key", name) is None
    default = await lease.credential()
    assert default is not None and default == await lease.credentials()  # прежний доступ — default

    active = await lease.active_credentials()
    assert sorted(active) == [("api_key", "trading"), ("default", "default")]

    await lease.release()
    with pytest.raises(LeaseLost):
        await lease.credential("api_key", "trading")


async def test_reveal_credential_by_kind_is_audited(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _setup(hub_under_test, space)

    value = await space.secrets.reveal(
        identity_id, SecretRef.credential("api_key", "trading"), actor="admin", reason="проверка"
    )

    assert value == {"secret": "s-trading"}
    page = await space.admin.event_page(
        filters=EventFilters(type="identity.secret.revealed", identity_id=identity_id)
    )
    (event,) = page.items
    assert (event.data["kind"], event.data["name"]) == ("api_key", "trading")
    assert "s-trading" not in repr(event)
    assert str(SecretRef.credential("api_key", "trading")) == "credential:api_key/trading"
