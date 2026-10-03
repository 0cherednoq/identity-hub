"""P2.5: изменение данных аккаунта и identity_data (ТЗ FR-3.1–3.6, FR-4.2).

Проверяется строка хранилища (`search_text`, шифрованная часть профиля), поэтому тесты
вызывают приватные реализации `space._set_labels` и т.п.; публичный API — `space.identities`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.crypto import associated_data
from identity_hub.errors import ConcurrentModification, NotFound, ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord, IdentityRecord
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


async def _events(sm: async_sessionmaker[AsyncSession]) -> list[EventRecord]:
    async with sm() as s:
        return list((await s.execute(select(EventRecord).order_by(EventRecord.id))).scalars())


async def test_labels_tags_note(space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    r = await register(space, service="fake_tg", external_ref="+1", labels={"farm": "3", "old": 1})
    row = await space._set_labels(r.identity_id, {"level": 2, "old": None}, merge=True, actor="admin")
    assert row.labels == {"farm": "3", "level": 2}
    assert "level:2" in row.search_text and "old:" not in row.search_text
    row = await space._set_labels(r.identity_id, {"only": True})
    assert row.labels == {"only": True}
    row = await space._add_tags(r.identity_id, ["b", "a"])
    row = await space._remove_tags(r.identity_id, ["b"])
    assert row.tags == ["a"]
    row = await space._set_note(r.identity_id, "Проверить  в пятницу", actor="admin")
    assert "проверить в пятницу" in row.search_text
    unchanged = await space._set_note(r.identity_id, "Проверить  в пятницу")
    assert unchanged.updated_at == row.updated_at

    updates = [e for e in await _events(sessionmaker) if e.type == "identity.updated"]
    assert [e.data["fields"] for e in updates] == [["labels"], ["labels"], ["tags"], ["tags"], ["note"]]
    assert updates[0].data["labels"] == {"farm": "3", "level": 2}
    assert updates[0].actor == "admin"
    with pytest.raises(ValidationFailed):
        await space._set_labels(r.identity_id, {"bad": [1]})


async def test_profile_merge_and_attributes(space: SpaceHandle) -> None:
    r = await register(
        space, service="fake_tg", external_ref="+1", profile={"first_name": "Olena", "country": "UA"}
    )
    row = await space._update_profile(r.identity_id, {"last_name": "Shevchenko"})
    assert row.profile == {"first_name": "Olena", "last_name": "Shevchenko", "country": "UA"}
    assert "shevchenko" in row.search_text
    row = await space._update_profile(r.identity_id, {"country": "PL"}, replace=True)
    assert row.profile == {"country": "PL"}
    with pytest.raises(ValidationFailed):
        await space._update_profile(r.identity_id, {"birthday": "not a date"})
    row = await space._set_attributes(r.identity_id, {"device": "iphone"})
    assert row.attributes == {"device": "iphone", "app_version": "10.0"}


async def test_encrypted_profile_update(
    hub: IdentityHub, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    hub.register_service(replace(FAKE_TG, name="secret_tg", profile_encrypted=True))
    await hub.start()
    space = hub.space("s", create=True)
    r = await register(
        space, service="secret_tg", external_ref="+9", profile={"first_name": "Olena", "country": "UA"}
    )
    row = await space._update_profile(r.identity_id, {"last_name": "K"})
    assert row.profile == {"country": "UA"}
    async with space._tx() as tx:
        payload = await tx.get_profile_secret(r.identity_id)
    assert payload is not None
    secret = hub._envelope.open_json(payload, associated_data("ih_identities", r.identity_id, "profile"))
    assert secret == {"first_name": "Olena", "last_name": "K"}


async def test_delete_restricted_by_links(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    mail = await register(space, service="fake_exchange", external_ref="m@x")
    tg = await register(
        space, service="fake_tg", external_ref="+1", links={"recovery_email": mail.identity_id}
    )
    with pytest.raises(ValidationFailed):
        await space.identities.delete(mail.identity_id, actor="test")
    await space.identities.delete(tg.identity_id, actor="admin")
    await space.identities.delete(mail.identity_id, actor="test")
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count()).select_from(IdentityRecord))).scalar_one() == 0
    deleted = [e for e in await _events(sessionmaker) if e.type == "identity.deleted"]
    assert {e.identity_id for e in deleted} == {tg.identity_id, mail.identity_id}  # журнал пережил удаление
    with pytest.raises(NotFound):
        await space.identities.delete(mail.identity_id, actor="test")


class Farm(BaseModel):
    coins: int = 0
    level: int = 1


async def test_identity_data_optimistic_lock(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    r = await register(space, service="fake_tg", external_ref="+1")
    assert await space.data.load(r.identity_id, "farm.notcoin", Farm) is None
    v1 = await space.data.save(r.identity_id, "farm.notcoin", Farm(coins=5), expected_version=None)
    assert v1 == 1
    with pytest.raises(ConcurrentModification):
        await space.data.save(r.identity_id, "farm.notcoin", Farm(coins=6), expected_version=None)
    record = await space.data.load(r.identity_id, "farm.notcoin", Farm)
    assert record is not None and record.value.coins == 5
    v2 = await space.data.save(r.identity_id, "farm.notcoin", Farm(coins=7), expected_version=record.version)
    assert v2 == 2
    with pytest.raises(ConcurrentModification):
        await space.data.save(r.identity_id, "farm.notcoin", Farm(coins=8), expected_version=record.version)
    with pytest.raises(ValidationFailed):
        await space.data.load(r.identity_id, "farm.notcoin", Farm, schema_version=2)
    with pytest.raises(ValidationFailed):
        await space.data.save(r.identity_id, "Bad Namespace", Farm(), expected_version=None)


async def test_identity_data_in_user_transaction(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    r = await register(space, service="fake_tg", external_ref="+1")
    async with sessionmaker() as user:
        await user.begin()
        await space.using(user).data.save(r.identity_id, "farm.x", Farm(coins=1), expected_version=None)
        await user.rollback()
    assert await space.data.load(r.identity_id, "farm.x", Farm) is None
