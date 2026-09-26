"""P2.4: register (ТЗ FR-2.1–2.3, FR-3.2, FR-4.2, FR-5.1, G-4.3)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub._ids import new_id
from identity_hub.crypto import EncryptedPayload, associated_data
from identity_hub.errors import AlreadyExists, NotFound, ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import (
    CredentialsRecord,
    EventRecord,
    IdentityLinkRecord,
    IdentityRecord,
    RegionStateRecord,
    SessionRecord,
)
from tests.fixtures.services import FAKE_TG, TgCredentials, TgSession

pytestmark = pytest.mark.pg


async def _one(sm: async_sessionmaker[AsyncSession], model: Any, identity_id: UUID) -> Any:
    async with sm() as s:
        return (await s.execute(select(model).where(model.identity_id == identity_id))).scalars().all()


async def _count(sm: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sm() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


async def test_register_full(
    space: SpaceHandle, hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    result = await space.register(
        service="fake_tg",
        external_ref="+380501112233",
        credentials=TgCredentials(phone="+380501112233", password=SecretStr("hunter2")),
        session={"auth_key": "AAAA", "dc_id": 4},
        profile={"first_name": "Olena", "country": "UA"},
        labels={"farm": "3"},
        tags=["parser", "ua"],
        note="куплен",
    )
    assert result.created
    async with sessionmaker() as s:
        row = (
            await s.execute(select(IdentityRecord).where(IdentityRecord.id == result.identity_id))
        ).scalar_one()
    assert (row.lifecycle, row.standing, row.auth_state) == ("provisioning", "good", "authenticated")
    assert row.tags == ["parser", "ua"]
    assert row.profile == {"first_name": "Olena", "country": "UA"}
    assert row.labels == {"farm": "3"}
    assert "olena" in row.search_text and "farm:3" in row.search_text
    assert row.event_seq == 1

    (creds,) = await _one(sessionmaker, CredentialsRecord, result.identity_id)
    assert b"hunter2" not in creds.ciphertext
    payload = EncryptedPayload(creds.key_id, creds.wrapped_dek, creds.nonce, creds.ciphertext)
    opened = hub.envelope.open_model(
        TgCredentials, payload, associated_data("ih_credentials", result.identity_id)
    )
    assert opened.password is not None and opened.password.get_secret_value() == "hunter2"

    (session_row,) = await _one(sessionmaker, SessionRecord, result.identity_id)
    assert session_row.slot == "default"
    assert session_row.written_by_fencing == 0
    session_payload = EncryptedPayload(
        session_row.key_id, session_row.wrapped_dek, session_row.nonce, session_row.ciphertext
    )
    restored = hub.envelope.open_model(
        TgSession, session_payload, associated_data("ih_sessions", result.identity_id, "default")
    )
    assert restored.auth_key.get_secret_value() == "AAAA"

    regions = {
        (r.region, r.key): r.state for r in await _one(sessionmaker, RegionStateRecord, result.identity_id)
    }
    assert regions == {("standing", ""): "good", ("auth", "default"): "authenticated"}

    async with sessionmaker() as s:
        event = (await s.execute(select(EventRecord))).scalar_one()
    assert event.type == "identity.registered"
    assert "hunter2" not in str(event.data) and "AAAA" not in str(event.data)


async def test_register_without_session_is_no_session(space: SpaceHandle) -> None:
    result = await space.register(service="fake_tg", external_ref="a")
    async with space._tx() as tx:
        row = await tx.get_identity(result.identity_id)
    assert row is not None and row.auth_state == "no_session"


async def test_register_is_idempotent(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    custom = new_id()
    first = await space.register(id=custom, service="fake_tg", external_ref="ref-1", labels={"a": "1"})
    assert first.identity_id == custom
    by_id = await space.register(id=custom, service="fake_tg", external_ref="other", labels={"a": "2"})
    by_ref = await space.register(service="fake_tg", external_ref="ref-1", labels={"a": "3"})
    assert (by_id.created, by_ref.created) == (False, False)
    assert by_id.identity_id == by_ref.identity_id == custom
    assert await _count(sessionmaker, IdentityRecord) == 1
    assert await _count(sessionmaker, EventRecord) == 1
    async with space._tx() as tx:
        row = await tx.get_identity(custom)
    assert row is not None and row.labels == {"a": "1"}


async def test_external_ref_unique_across_spaces(hub: IdentityHub, space: SpaceHandle) -> None:
    await space.register(service="fake_tg", external_ref="+1")
    with pytest.raises(AlreadyExists):
        await hub.space("other").register(service="fake_tg", external_ref="+1")
    other = await hub.space("other").register(service="fake_exchange", external_ref="+1")
    assert other.created


async def test_validation_before_db(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ValidationFailed):
        await space.register(service="fake_tg", external_ref="x", credentials={"password": "no phone"})
    with pytest.raises(ValidationFailed):
        await space.register(service="fake_tg", external_ref="x", labels={"nested": {"a": 1}})
    with pytest.raises(ValidationFailed):
        await space.register(service="fake_tg", external_ref="x", profile={"country": {"bad": 1}})
    assert await _count(sessionmaker, IdentityRecord) == 0


async def test_links_same_space_only(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    email = await space.register(service="fake_exchange", external_ref="mail@x")
    tg = await space.register(
        service="fake_tg", external_ref="+2", links={"recovery_email": email.identity_id}
    )
    links = await _one(sessionmaker, IdentityLinkRecord, tg.identity_id)
    assert [(link.role, link.linked_identity_id) for link in links] == [("recovery_email", email.identity_id)]

    foreign = await hub.space("other").register(service="fake_exchange", external_ref="mail@y")
    with pytest.raises(NotFound):
        await space.register(
            service="fake_tg", external_ref="+3", links={"recovery_email": foreign.identity_id}
        )
    assert await _count(sessionmaker, IdentityRecord) == 3  # +3 откатился целиком
    self_id = new_id()
    with pytest.raises(ValidationFailed):
        await space.register(id=self_id, service="fake_tg", external_ref="+4", links={"x": self_id})


async def test_register_in_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await space.ref()
    async with sessionmaker() as user:
        await user.begin()
        await space.using(user).register(
            service="fake_tg", external_ref="shared", credentials={"phone": "+1"}
        )
        await user.rollback()
    assert await _count(sessionmaker, IdentityRecord) == 0
    assert await _count(sessionmaker, CredentialsRecord) == 0
    assert await _count(sessionmaker, EventRecord) == 0

    async with sessionmaker() as user, user.begin():
        result = await space.using(user).register(service="fake_tg", external_ref="shared")
    assert result.created
    assert await _count(sessionmaker, IdentityRecord) == 1


async def test_encrypted_profile(hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    secret_tg = replace(FAKE_TG, name="secret_tg", profile_encrypted=True)
    hub.register_service(secret_tg)
    await hub.start()
    result = await hub.space("s").register(
        service="secret_tg", external_ref="+9", profile={"first_name": "Olena", "country": "UA"}
    )
    async with sessionmaker() as s:
        row = (
            await s.execute(select(IdentityRecord).where(IdentityRecord.id == result.identity_id))
        ).scalar_one()
    assert row.profile == {"country": "UA"}
    assert "olena" not in row.search_text
    assert row.profile_ciphertext is not None
    payload = EncryptedPayload(
        row.profile_key_id, row.profile_wrapped_dek, row.profile_nonce, row.profile_ciphertext
    )
    secret = hub.envelope.open_json(payload, associated_data("ih_identities", result.identity_id, "profile"))
    assert secret == {"first_name": "Olena"}
