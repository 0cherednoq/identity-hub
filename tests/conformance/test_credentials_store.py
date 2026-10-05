"""IMP-09 / R2-P08 (I3.1a): несколько credentials в Store, версии MemoryStore (D-214)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle, ValidationFailed
from identity_hub.clock import FakeClock
from identity_hub.crypto import credential_associated_data

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


class ApiKey(BaseModel):
    secret: SecretStr


async def _identity(space: SpaceHandle, service: str = "fake_tg") -> UUID:
    created = await space.identities.create(
        IdentityCreate(
            service=service,
            external_ref="+1",
            credentials={"phone": "+1", "password": "p0"},
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    return created.id


async def _tick(hub: IdentityHub) -> None:
    if isinstance(hub.clock, FakeClock):
        hub.clock.advance(timedelta(seconds=1))
    else:
        await asyncio.sleep(0.01)


async def test_describe_versions_grow_on_both_stores(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """R2-P08: версии и время изменения credentials и сессий одинаковы в MemoryStore и PostgreSQL."""
    identity_id = await _identity(space)
    before = await space.secrets.describe(identity_id)
    assert before.credential.version == 1 and before.credential.updated_at is not None
    (session,) = before.sessions
    assert session.version == 1 and session.updated_at is not None

    await _tick(hub_under_test)
    await space.secrets.replace_credentials(identity_id, {"phone": "+1", "password": "p1"}, actor="ops")
    await space.secrets.replace_session(identity_id, {"auth_key": "k2"}, actor="ops")
    after = await space.secrets.describe(identity_id)

    assert after.credential.version == 2 and after.credential.updated_at > before.credential.updated_at
    assert after.sessions[0].version == 2 and after.sessions[0].updated_at > session.updated_at


async def test_several_credentials_of_one_kind(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    hub_under_test.register_service(replace(FAKE_TG, name="fake_keys", credential_kinds={"api_key": ApiKey}))
    await hub_under_test.start()
    identity_id = await _identity(space, "fake_keys")
    envelope = hub_under_test._envelope

    for name, secret in (("trading", "s-trade"), ("readonly", "s-read")):
        payload = envelope.seal_model(
            ApiKey(secret=SecretStr(secret)), credential_associated_data(identity_id, "api_key", name)
        )
        async with space._tx() as tx:
            await tx.put_credential(identity_id, "api_key", name, payload, public_value={"key_id": name})

    async with space._tx(own=True) as tx:
        rows = await tx.list_credentials(identity_id)
        found = await tx.get_credential(identity_id, "api_key", "readonly")
    assert sorted((row.kind, row.name) for row in rows) == [
        ("api_key", "readonly"),
        ("api_key", "trading"),
        ("default", "default"),
    ]
    assert {row.name: row.public_value for row in rows if row.kind == "api_key"} == {
        "trading": {"key_id": "trading"},
        "readonly": {"key_id": "readonly"},
    }
    assert all(row.lifecycle == "active" and row.revision == 1 for row in rows)
    assert found is not None
    opened = envelope.open_model(
        ApiKey, found[0], credential_associated_data(identity_id, "api_key", "readonly")
    )
    assert opened.secret.get_secret_value() == "s-read"
    assert "s-read" not in repr(rows)

    other = hub_under_test.space("other-tenant", create=True)
    async with other._tx(own=True) as tx:  # изоляция пространств
        assert await tx.list_credentials(identity_id) == []
        assert await tx.get_credential(identity_id, "api_key", "readonly") is None


def test_service_rejects_unknown_kind_and_bad_name() -> None:
    service = replace(FAKE_TG, credential_kinds={"api_key": ApiKey})

    assert service.credential_schema("default") is service.credentials
    assert service.credential_schema("api_key") is ApiKey
    with pytest.raises(ValidationFailed):
        service.credential_schema("totp")
    with pytest.raises(ValidationFailed):
        service.credential_key("api_key", "")
    with pytest.raises(ValidationFailed):
        service.credential_key("api_key", "x" * 129)
