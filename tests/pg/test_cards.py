"""P6.2: карточка, сводка, история и статистика аренд, переиндексация (ТЗ FR-11.5–11.8)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import IdentityRecord
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


class Farm(BaseModel):
    coins: int = 0


async def test_identity_card(space: SpaceHandle) -> None:
    mail = await register(space, service="fake_exchange", external_ref="m@x", lifecycle="ready")
    r = await register(
        space,
        service="fake_tg",
        external_ref="+1",
        lifecycle="ready",
        session={"auth_key": "SECRET-KEY"},
        credentials={"phone": "+1", "password": "SECRET-PW"},
        links={"recovery_email": mail.identity_id},
    )
    await space.data.save(r.identity_id, "farm.notcoin", Farm(coins=1), expected_version=None)
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[r.identity_id])
    async with lease.operation("read_channel"):
        pass

    card = await space.identities.card(r.identity_id)
    assert card.identity.external_ref == "+1"
    assert {(i.region, i.key) for i in card.regions} >= {
        ("lifecycle", ""),
        ("availability", ""),
        ("standing", ""),
        ("auth", "default"),
    }
    assert card.lease is not None and card.lease.lease_id == lease.lease_id
    assert [link.role for link in card.links.outgoing] == ["recovery_email"]
    assert [(q.kind, q.used) for q in card.quotas] == [("read_channel", 1)]
    assert [item.namespace for item in card.data_namespaces] == ["farm.notcoin"]
    assert card.data_namespaces[0].schema_version == 1
    assert card.data_namespaces[0].version == 1
    assert card.lease.heartbeat_at is not None
    access = await space.secrets.describe(r.identity_id)
    assert access.credential.present is True
    assert access.credential.version == 1
    assert access.sessions[0].slot == "default"
    assert not hasattr(access.credential, "ciphertext")
    assert card.admin_url == f"https://admin.test/identities/{r.identity_id}"
    dump = repr(card)
    assert "SECRET" not in dump and str(lease._token) not in dump


async def test_pool_summary(space: SpaceHandle) -> None:
    await register(space, service="fake_tg", external_ref="+1", lifecycle="ready")
    await register(space, service="fake_tg", external_ref="+2")
    await register(space, service="fake_exchange", external_ref="m@x")
    summary = await space.admin.pool_summary()
    assert summary["fake_tg"]["lifecycle"] == {"ready": 1, "provisioning": 1}
    assert summary["fake_tg"]["auth"] == {"no_session": 2}
    assert summary["fake_exchange"]["standing"] == {"good": 1}
    assert set(await space.admin.pool_summary("fake_exchange")) == {"fake_exchange"}


async def test_lease_history_and_stats(space: SpaceHandle) -> None:
    r = await register(
        space, service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    pool = space.pools.define("p", service="fake_tg")
    for _ in range(3):
        async with pool.lease() as lease, lease.operation("read_channel"):
            pass
    with pytest.raises(RuntimeError):
        async with pool.lease():
            raise RuntimeError("fail")
    history = await space.admin.lease_history(r.identity_id)
    assert [h.end_reason for h in history] == ["failed", "released", "released", "released"]
    assert not hasattr(history[0], "token")
    async with space._tx() as tx:
        since = await tx.now() - timedelta(hours=1)
    stats = await space.admin.lease_stats(r.identity_id, since)
    assert stats.count == 4
    assert stats.by_reason == {"released": 3, "failed": 1}
    assert stats.operations == {"read_channel": 3}
    assert (stats.succeeded, stats.failed) == (3, 0)
    assert 0 <= stats.utilization <= 1 and stats.longest is not None
    dashboard = await space.admin.lease_dashboard(group_by="holder")
    assert dashboard.windows[-1].completed == 4
    assert dashboard.windows[-1].p95_seconds is not None
    assert dashboard.slices[0].value == history[0].holder
    page = await space.admin.lease_history_page(limit=2)
    assert len(page.items) == 2 and page.next_before is not None
    assert not hasattr(page.items[0], "token")


async def test_reindex_search(
    hub: IdentityHub, space: SpaceHandle, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    r = await register(
        space, service="fake_tg", external_ref="+1", profile={"first_name": "Olena", "country": "UA"}
    )
    async with sessionmaker() as s, s.begin():
        await s.execute(update(IdentityRecord).values(search_text="stale"))
    assert await space.maintenance.reindex_search("fake_tg") == 1
    assert {row.id for row in (await space.identities.search("olena")).items} == {r.identity_id}
    hub._services["fake_tg"] = replace(hub.service("fake_tg"), searchable_profile=frozenset({"country"}))
    assert await space.maintenance.reindex_search("fake_tg", batch_size=1) == 1
    assert (await space.identities.search("olena")).items == []
