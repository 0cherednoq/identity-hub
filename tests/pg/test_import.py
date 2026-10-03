"""P5.7: import_many (ТЗ FR-2.4–2.7, G-4.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.importing import ImportRecord
from identity_hub.postgres.models import (
    BindingRecord,
    EventRecord,
    IdentityLinkRecord,
    IdentityRecord,
    ProxyRecord,
)
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


def rec(ref: str, **kwargs: Any) -> ImportRecord:
    return ImportRecord(external_ref=ref, credentials={"phone": ref}, **kwargs)


async def _count(sm: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sm() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


async def _identity(sm: async_sessionmaker[AsyncSession], ref: str) -> Any:
    async with sm() as s:
        return (
            await s.execute(select(IdentityRecord).where(IdentityRecord.external_ref == ref))
        ).scalar_one()


async def test_import_mixed_batch_and_repeat(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    records = [
        rec("+1", sessions={"default": {"auth_key": "k1"}}, labels={"farm": "3"}),
        rec("+2"),
        ImportRecord(external_ref="+3", credentials={"password": "no phone"}),  # не пройдёт схему
        rec("+1"),  # дубликат в пачке
    ]
    report = await space.imports.run(service="fake_tg", records=records, batch_size=2)
    assert len(report.created) == 2
    assert [ref for ref, _ in report.failed] == ["+3"]
    assert report.skipped == [("+1", "уже существует")]  # вторая пачка: +1 создан первой
    one = await _identity(sessionmaker, "+1")
    two = await _identity(sessionmaker, "+2")
    assert (one.lifecycle, one.auth_state, one.labels) == ("provisioning", "authenticated", {"farm": "3"})
    assert two.auth_state == "no_session"
    async with sessionmaker() as s:
        imported = list(
            (await s.execute(select(EventRecord).where(EventRecord.type == "identity.imported"))).scalars()
        )
    assert sum(e.data["count"] for e in imported) == 2

    again = await space.imports.run(service="fake_tg", records=[rec("+1"), rec("+2")])
    assert again.created == [] and [r for r, _ in again.skipped] == ["+1", "+2"]
    assert await _count(sessionmaker, IdentityRecord) == 2


async def test_verify_sessions_and_update_session(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await space.imports.run(
        service="fake_tg",
        records=[rec("+1", sessions={"default": {"auth_key": "old"}})],
        verify_sessions=True,
    )
    assert (await _identity(sessionmaker, "+1")).auth_state == "no_session"
    report = await space.imports.run(
        service="fake_tg",
        records=[rec("+1", sessions={"default": {"auth_key": "new"}})],
        on_existing="update_session",
    )
    assert len(report.updated) == 1
    identity = await _identity(sessionmaker, "+1")
    assert identity.auth_state == "authenticated"
    assert (await space.secrets.reveal(identity.id, "session", actor="t", reason="тест"))["auth_key"] == "new"
    failing = await space.imports.run(service="fake_tg", records=[rec("+1")], on_existing="fail")
    assert failing.failed == [("+1", "уже существует")]


async def test_links_within_batch_and_across_services(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await register(space, service="fake_exchange", external_ref="mail@x")
    report = await space.imports.run(
        service="fake_tg",
        records=[
            rec("+1", links={"recovery_email": "fake_exchange:mail@x", "partner": "+2"}),
            rec("+2"),
        ],
    )
    assert len(report.created) == 2, report.failed
    assert await _count(sessionmaker, IdentityLinkRecord) == 2
    broken = await space.imports.run(
        service="fake_tg", records=[rec("+9", links={"x": "fake_exchange:nobody"})]
    )
    assert broken.created == [] and "не найден" in broken.failed[0][1]  # пачка отклонена целиком
    assert await _count(sessionmaker, IdentityRecord) == 3


async def test_proxies_and_verifications(
    space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    report = await space.imports.run(
        service="fake_exchange",
        records=[
            ImportRecord(
                external_ref="a@x", proxy="socks5://u:p@1.1.1.1:1080", verifications={"email": "verified"}
            ),
            ImportRecord(external_ref="b@x", proxy="socks5://u:p@1.1.1.1:1080"),
        ],
    )
    assert len(report.created) == 2
    assert await _count(sessionmaker, ProxyRecord) == 1  # дедупликация по fingerprint
    assert await _count(sessionmaker, BindingRecord) == 2
    async with sessionmaker() as s:
        assert (await s.execute(select(ProxyRecord.bound_count))).scalar_one() == 2
    assert (await _identity(sessionmaker, "a@x")).verified_factors == ["email"]


async def test_async_stream_hook_and_other_space(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seen: list[list[UUID]] = []

    async def hook(session: AsyncSession, ids: list[UUID]) -> None:
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        seen.append(ids)

    async def stream() -> AsyncIterator[ImportRecord]:
        for i in range(5):
            yield rec(f"+{i}")

    report = await space.imports.run(
        service="fake_tg", records=stream(), batch_size=2, on_batch_committed=hook
    )
    assert len(report.created) == 5
    assert [len(batch) for batch in seen] == [2, 2, 1]
    other = await hub.space("other", create=True).imports.run(service="fake_tg", records=[rec("+0")])
    assert "другом пространстве" in other.failed[0][1]


async def test_file_importer(hub: IdentityHub, space: SpaceHandle, tmp_path: Path) -> None:
    from dataclasses import replace

    from tests.fixtures.services import FAKE_TG

    class JsonImporter:
        def can_import(self, path: Path) -> bool:
            return path.suffix == ".json"

        async def read(self, path: Path) -> AsyncIterator[ImportRecord]:
            import json

            for item in json.loads(path.read_text(encoding="utf-8")):  # noqa: ASYNC240 - тест
                yield rec(item["phone"], sessions={"default": {"auth_key": item["key"]}})

    hub.register_service(replace(FAKE_TG, name="tg_files", importers=(JsonImporter(),)))
    await hub.start()
    path = tmp_path / "accounts.json"
    path.write_text('[{"phone": "+7", "key": "a"}, {"phone": "+8", "key": "b"}]', encoding="utf-8")
    report = await space.imports.run_files(service="tg_files", paths=[path])
    assert len(report.created) == 2
