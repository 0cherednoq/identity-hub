"""P5.6: секреты для админки (ТЗ FR-5.2, S-7, S-8)."""

from __future__ import annotations

from dataclasses import fields

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.hub import SpaceHandle
from identity_hub.postgres.models import EventRecord
from identity_hub.store.base import IdentityRow

pytestmark = pytest.mark.pg


async def test_update_and_reveal_with_audit(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    r = await space.register(
        service="fake_tg",
        external_ref="+1",
        credentials={"phone": "+1", "password": "old"},
        session={"auth_key": "K"},
    )
    await space.update_credentials(r.identity_id, {"phone": "+1", "password": "new"}, actor="admin")
    creds = await space.reveal_secret(r.identity_id, "credentials", actor="evgeny", reason="поддержка")
    assert creds == {"phone": "+1", "password": "new"}
    assert (await space.reveal_secret(r.identity_id, "session", actor="evgeny"))["auth_key"] == "K"
    with pytest.raises(NotFound):
        await space.reveal_secret(r.identity_id, "session:backup", actor="evgeny")
    with pytest.raises(ValidationFailed):
        await space.reveal_secret(r.identity_id, "token", actor="evgeny")

    async with sessionmaker() as s:
        events = list((await s.execute(select(EventRecord).order_by(EventRecord.id))).scalars())
    revealed = [e for e in events if e.type == "identity.secret.revealed"]
    assert [(e.actor, e.data["field"]) for e in revealed] == [
        ("evgeny", "credentials"),
        ("evgeny", "session"),
    ]
    assert revealed[0].data["reason"] == "поддержка"
    assert any(e.type == "identity.credentials.updated" for e in events)
    assert all("new" not in str(e.data) and e.data.get("auth_key") != "K" for e in events)


def test_read_models_have_no_secret_fields() -> None:
    names = {f.name for f in fields(IdentityRow)}
    assert not names & {"ciphertext", "wrapped_dek", "profile_ciphertext", "token", "password"}
