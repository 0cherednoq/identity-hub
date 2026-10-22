"""C8.1 (`D-326`): пометка валидности credentials и сессий оператором вне аренды."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityCreate, IdentityHub, SpaceHandle, ValidationFailed
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.conformance]


class ApiKey(BaseModel):
    secret: SecretStr


async def _identity(hub: IdentityHub, space: SpaceHandle) -> UUID:
    hub.register_service(
        replace(
            FAKE_TG,
            name="fake_validity",
            credential_kinds={"api_key": ApiKey},
            disable_rejected_credentials=False,
        )
    )
    await hub.start()
    created = await space.identities.create(
        IdentityCreate(
            service="fake_validity",
            external_ref=f"+{uuid4().hex[:10]}",
            credentials={"phone": "+1", "password": "p"},
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    await space.secrets.put_credential(created.id, "api_key", {"secret": "s"}, name="trading", actor="ops")
    return created.id


async def _credential(space: SpaceHandle, identity_id: UUID, name: str = "trading") -> object:
    found = [c for c in await space.secrets.credentials(identity_id) if c.name == name]
    assert found, name
    return found[0]


async def test_verify_and_reject_credential_record_outcome(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(hub_under_test, space)
    before = await space.identities.get(identity_id)

    verified = await space.secrets.verify_credential(identity_id, "api_key", "trading", actor="ops")
    assert verified is not None and verified.last_verified_at is not None
    assert verified.last_rejected_at is None and verified.reject_code is None
    assert verified.lifecycle == "active" and verified.revision > 1

    rejected = await space.secrets.reject_credential(
        identity_id, "api_key", "trading", actor="ops", code="bad_password"
    )
    assert rejected is not None and rejected.reject_code == "bad_password"
    assert rejected.last_rejected_at is not None and rejected.last_verified_at is None
    assert rejected.lifecycle == "active"  # disable_rejected_credentials=False

    # повторная отметка verify снимает отказ
    again = await space.secrets.verify_credential(identity_id, "api_key", "trading", actor="ops")
    assert again is not None and again.reject_code is None and again.last_rejected_at is None

    # регион auth аккаунта пометка не трогает (`D-326` п. г)
    after = await space.identities.get(identity_id)
    assert after.auth == before.auth == "authenticated"


async def test_reject_credential_can_disable_record(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _identity(hub_under_test, space)
    rejected = await space.secrets.reject_credential(
        identity_id, "api_key", "trading", actor="ops", code="bad_password", disable=True
    )
    assert rejected is not None and rejected.lifecycle == "disabled"

    # отозванную запись отметка не поднимает и не ломает
    assert await space.secrets.revoke_credential(identity_id, "api_key", "trading", actor="ops")
    still = await space.secrets.reject_credential(
        identity_id, "api_key", "trading", actor="ops", code="bad_password", disable=True
    )
    assert still is not None and still.lifecycle == "revoked"


async def test_missing_credential_returns_none_and_bad_code_is_rejected(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(hub_under_test, space)
    assert await space.secrets.verify_credential(identity_id, "api_key", "absent", actor="ops") is None
    assert (
        await space.secrets.reject_credential(
            identity_id, "api_key", "absent", actor="ops", code="bad_password"
        )
        is None
    )
    with pytest.raises(ValidationFailed):
        await space.secrets.reject_credential(
            identity_id, "api_key", "trading", actor="ops", code="Bad Password!"
        )


async def test_verify_and_reject_session(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    identity_id = await _identity(hub_under_test, space)

    assert await space.secrets.verify_session(identity_id, actor="ops")
    card = await space.secrets.describe(identity_id)
    current = next(s for s in card.sessions if s.status != "revoked")
    assert current.last_verified_at is not None and current.reject_code is None

    assert await space.secrets.reject_session(identity_id, actor="ops", code="session_invalid")
    card = await space.secrets.describe(identity_id)
    current = next(s for s in card.sessions if s.status != "revoked")
    assert current.reject_code == "session_invalid" and current.last_rejected_at is not None
    assert current.last_verified_at is None

    # статус записи и auth аккаунта пометка не меняет
    assert current.status == "active"
    assert (await space.identities.get(identity_id)).auth == "authenticated"

    assert not await space.secrets.verify_session(identity_id, slot="absent", actor="ops")


async def test_marks_are_audited_in_existing_event_types(
    hub_under_test: IdentityHub, space: SpaceHandle
) -> None:
    identity_id = await _identity(hub_under_test, space)
    await space.secrets.verify_credential(identity_id, "api_key", "trading", actor="ops")
    await space.secrets.reject_credential(identity_id, "api_key", "trading", actor="ops", code="bad_password")
    await space.secrets.reject_session(identity_id, actor="ops", code="session_invalid")

    page = await space.admin.event_page(filters=EventFilters(identity_id=identity_id), limit=50)
    credential_actions = [
        event.data.get("action") for event in page.items if event.type == "identity.credentials.updated"
    ]
    session_modes = [
        event.data.get("mode") for event in page.items if event.type == "identity.session.updated"
    ]
    assert "verify" in credential_actions and "reject" in credential_actions
    assert "reject" in session_modes
    rejected = next(
        event
        for event in page.items
        if event.type == "identity.credentials.updated" and event.data.get("action") == "reject"
    )
    assert rejected.data["code"] == "bad_password" and rejected.actor == "ops"
