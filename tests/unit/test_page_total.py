"""R1-28: `Page.total` у `list`/`search` ограничен `COUNT_LIMIT`, точное число — `identities.count()`."""

from __future__ import annotations

import pytest

from identity_hub import IdentityCreate, IdentityHub, StaticSecretsProvider
from identity_hub.api.identities import Page
from identity_hub.read.search import COUNT_LIMIT
from identity_hub.testing import MemoryStore, make_service

pytestmark = pytest.mark.unit


async def test_page_total_is_capped_and_count_is_exact() -> None:
    svc = make_service("svc")
    hub = IdentityHub(
        store=MemoryStore(), secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"), services=[svc]
    )
    await hub.start()
    space = hub.space("s", "S", create=True)
    created = COUNT_LIMIT + 2
    for start in range(0, created, 1000):
        specs = [
            IdentityCreate(service=svc, external_ref=f"+{n}")
            for n in range(start, min(created, start + 1000))
        ]
        await space.identities.create_many(specs, actor="test")

    listed = await space.identities.list(limit=1, count=True)
    searched = await space.identities.search("", limit=1, count=True)

    assert listed.total == searched.total == COUNT_LIMIT + 1  # «больше COUNT_LIMIT»
    assert await space.identities.count() == created
    assert "COUNT_LIMIT" in (Page.__doc__ or "")
