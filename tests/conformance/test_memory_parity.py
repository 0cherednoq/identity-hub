"""F5.2a: MemoryStore ведёт себя как PostgreSQL (R2-P06, R2-P07, R2-P10, R2-P12, R2-P14, R2-P18, R2-F26)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG, KycEvidence

from identity_hub import (
    AuthState,
    IdentityCreate,
    IdentityHub,
    Lifecycle,
    ProxyPolicy,
    SpaceHandle,
    StaticSecretsProvider,
)
from identity_hub.clock import FakeClock
from identity_hub.errors import LeaseUnavailable
from identity_hub.read.events import EventFilters
from identity_hub.service import ProxyDownPolicy
from identity_hub.testing import MemoryStore

pytestmark = pytest.mark.conformance

TG_PROXY = replace(
    FAKE_TG,
    name="tg_proxy",
    proxy_policy=ProxyPolicy(group="res", sticky=True, on_proxy_down=ProxyDownPolicy.WAIT),
)


@pytest.fixture(params=["memory", "postgres"], ids=["memory", "postgres"])
async def hub(
    request: pytest.FixtureRequest, sessionmaker: async_sessionmaker[AsyncSession]
) -> AsyncIterator[IdentityHub]:
    secrets = StaticSecretsProvider(TEST_KEYS, "k1")
    services = [FAKE_TG, FAKE_EXCHANGE, TG_PROXY]
    if request.param == "memory":
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        instance = IdentityHub(secrets=secrets, services=services, store=MemoryStore(clock), clock=clock)
    else:
        instance = IdentityHub(sessionmaker, secrets, services)
    await instance.start()
    yield instance


@pytest.fixture
def space(hub: IdentityHub) -> SpaceHandle:
    return hub.space(f"s-{uuid4().hex[:8]}")


async def _account(space: SpaceHandle, ref: str, service: str = "fake_tg", **fields: object) -> UUID:
    fields.setdefault("lifecycle", Lifecycle.READY)
    fields.setdefault("auth_state", AuthState.AUTHENTICATED)
    identity = await space.identities.create(IdentityCreate(service=service, external_ref=ref, **fields))
    return identity.id


async def test_deleting_account_frees_sticky_proxy_capacity(space: SpaceHandle) -> None:
    """R2-P06, R2-F26: удалённый аккаунт освобождает место на прокси."""
    await space.proxies.create("http://10.9.0.1:80", group="res", max_identities=1)
    first = await _account(space, "+1", service="tg_proxy")
    pool = space.pools.define("p", service="tg_proxy")
    async with pool.lease() as lease:
        used = lease.proxy_id
    await space.identities.delete(first, actor="t")
    await _account(space, "+2", service="tg_proxy")

    async with pool.lease() as lease:
        assert lease.proxy_id == used


async def test_verification_evidence_is_readable_after_submit(space: SpaceHandle) -> None:
    """R2-P07: доказательства верификации читаются и после `submit`."""
    identity = await space.identities.create(
        IdentityCreate(
            service="fake_exchange",
            external_ref="t@x",
            lifecycle=Lifecycle.READY,
            credentials={"email": "t@x", "password": "p"},
            session={"cookies": {"sid": "1"}},
        )
    )
    lease = await space.pools.define("ex", service="fake_exchange").acquire(among=[identity.id])
    try:
        for factor in ("email", "phone"):
            verification = lease.verification(factor)
            await verification.start()
            await verification.submit()
        kyc = lease.verification("kyc_id")
        await kyc.start(KycEvidence(document_number=SecretStr("AB123456")))
        await kyc.submit()

        evidence = await kyc.evidence()

        assert isinstance(evidence, KycEvidence)
        assert evidence.document_number.get_secret_value() == "AB123456"
    finally:
        await lease.release()


async def test_journal_order_is_strict_within_one_instant(space: SpaceHandle) -> None:
    """R2-P10: под FakeClock события одного момента идут в порядке записи, как на UUIDv7."""
    await _account(space, "+1")
    pool = space.pools.define("p", service="fake_tg")
    for _ in range(3):
        async with pool.lease():
            pass

    page = await space.admin.event_page(filters=EventFilters(include_journal=True))

    types = [item.type for item in page.items if item.type in {"lease.acquired", "lease.released"}]
    assert types == ["lease.released", "lease.acquired"] * 3
    history = await space.admin.lease_history_page(limit=10)
    lease_ids = [item.lease_id for item in history.items]
    assert len(lease_ids) == 3 and lease_ids == sorted(lease_ids, reverse=True)  # новые — первыми


async def test_profile_filter_none_matches_like_jsonb_containment(space: SpaceHandle) -> None:
    """R2-P12: `{"country": None}` не совпадает с аккаунтом, у которого поля нет."""
    await _account(space, "+1")
    await _account(space, "+2", profile={"country": "ru"})
    pool = space.pools.define("p", service="fake_tg")

    with pytest.raises(LeaseUnavailable):
        async with pool.lease(profile={"country": None}):
            pass


async def test_links_order_and_dates(space: SpaceHandle) -> None:
    """R2-P14: связи в одном порядке, `created_at` — время связи, а не другого аккаунта."""
    root = await _account(space, "+1")
    middle = await _account(space, "+2", links={"recovery": root})
    await _account(space, "+3", links={"owner": middle})
    impact = [(item.depth, list(item.path)) for item in await space.links.impact(root)]
    assert impact == [(1, ["recovery"]), (2, ["recovery", "owner"])]

    b, c = await _account(space, "+5"), await _account(space, "+6")
    a = await _account(space, "+4")
    await space.links.add(a, "zz", b)
    await space.links.add(a, "aa", c)
    outgoing = (await space.links.of(a)).outgoing
    assert [link.role for link in outgoing] == ["aa", "zz"]
    created = (await space.identities.get(c)).created_at
    assert all(link.created_at >= created for link in outgoing)


async def test_lease_stats_count_open_lease(space: SpaceHandle) -> None:
    """R2-P18: открытая аренда учитывается в статистике как `active`."""
    identity_id = await _account(space, "+1")
    pool = space.pools.define("p", service="fake_tg")

    async with pool.lease():
        stats = await space.admin.lease_stats(identity_id, datetime(2020, 1, 1, tzinfo=UTC))

    assert dict(stats.by_reason) == {"active": 1}
