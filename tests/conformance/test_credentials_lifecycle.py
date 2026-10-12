"""IMP-09 (I3.1b): жизненный цикл credentials, срок по времени и команды `space.secrets` (D-215)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle, TransitionNotAllowed, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.conformance]


class ApiKey(BaseModel):
    secret: SecretStr


async def _identity(hub: IdentityHub, space: SpaceHandle) -> UUID:
    hub.register_service(replace(FAKE_TG, name="fake_keys", credential_kinds={"api_key": ApiKey}))
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(service="fake_keys", external_ref="+1", credentials={"phone": "+1", "password": "p"}),
        actor="test",
    )
    return created.id


async def _states(space: SpaceHandle, identity_id: UUID) -> dict[tuple[str, str], str]:
    return {(c.kind, c.name): c.lifecycle for c in await space.secrets.credentials(identity_id)}


async def test_disable_enable_revoke(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _identity(hub_under_test, space)
    for name in ("trading", "readonly"):
        await space.secrets.put_credential(
            identity_id,
            "api_key",
            {"secret": f"s-{name}"},
            name=name,
            public_value={"key_id": name},
            actor="ops",
        )

    assert await space.secrets.disable_credential(identity_id, "api_key", "trading", actor="ops")
    assert not await space.secrets.disable_credential(identity_id, "api_key", "trading", actor="ops")
    assert (await _states(space, identity_id))[("api_key", "trading")] == "disabled"
    assert await space.secrets.enable_credential(identity_id, "api_key", "trading", actor="ops")
    assert await space.secrets.revoke_credential(
        identity_id, "api_key", "readonly", actor="ops", reason="утёк"
    )
    assert not await space.secrets.revoke_credential(identity_id, "api_key", "readonly", actor="ops")

    with pytest.raises(TransitionNotAllowed):
        await space.secrets.enable_credential(identity_id, "api_key", "readonly", actor="ops")
    with pytest.raises(TransitionNotAllowed):
        await space.secrets.put_credential(
            identity_id, "api_key", {"secret": "new"}, name="readonly", actor="ops"
        )
    assert await _states(space, identity_id) == {
        ("api_key", "readonly"): "revoked",
        ("api_key", "trading"): "active",
        ("default", "default"): "active",
    }
    trading = next(c for c in await space.secrets.credentials(identity_id) if c.name == "trading")
    assert trading.revision == 3 and trading.public_value == {"key_id": "trading"}  # put, disable, enable


async def test_expiry_is_by_time(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _identity(hub_under_test, space)
    now = hub_under_test.clock.now()
    await space.secrets.put_credential(
        identity_id,
        "api_key",
        {"secret": "s"},
        name="short",
        expires_at=now + timedelta(seconds=1),
        actor="ops",
    )
    (short,) = [c for c in await space.secrets.credentials(identity_id) if c.name == "short"]
    assert not short.expired

    if isinstance(hub_under_test.clock, FakeClock):
        hub_under_test.clock.advance(timedelta(seconds=2))
    else:
        await asyncio.sleep(1.2)

    (short,) = [c for c in await space.secrets.credentials(identity_id) if c.name == "short"]
    assert short.expired and short.lifecycle == "active"  # срок — по времени, не переходом


async def test_validation_and_secret_free_events(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _identity(hub_under_test, space)
    with pytest.raises(ValidationFailed):
        await space.secrets.put_credential(identity_id, "totp", {"secret": "x"}, actor="ops")
    with pytest.raises(ValidationFailed):
        await space.secrets.put_credential(identity_id, "api_key", {"wrong": "x"}, actor="ops")

    await space.secrets.put_credential(
        identity_id,
        "api_key",
        {"secret": "very-secret"},
        name="k",
        public_value={"key_id": "pub"},
        actor="ops",
    )
    await space.secrets.revoke_credential(identity_id, "api_key", "k", actor="ops", reason="ротация")

    page = await space.admin.event_page(
        filters=EventFilters(type="identity.credentials.updated", identity_id=identity_id)
    )
    actions = sorted(item.data.get("action") for item in page.items if item.data.get("kind") == "api_key")
    assert actions == ["put", "revoke"]
    assert all("very-secret" not in repr(item) and "pub" not in repr(item.data) for item in page.items)
    assert "very-secret" not in repr(await space.secrets.credentials(identity_id))
