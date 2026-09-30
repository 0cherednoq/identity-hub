"""A2.6: PostgreSQL secret-free proxy overview and detail projections."""

from __future__ import annotations

from dataclasses import replace

import pytest

from identity_hub.errors import NotFound
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.read.proxies import ProxyFilters
from identity_hub.service import ProxyPolicy
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.pg


async def test_proxy_counts_bindings_active_leases_history_and_scope(
    hub: IdentityHub,
    space: SpaceHandle,
) -> None:
    service = replace(
        FAKE_TG,
        name="proxy_read_pg",
        proxy_policy=ProxyPolicy(pool="res", max_identities=2),
    )
    hub.register_service(service)
    await hub.start()
    proxy_id, _ = await space.add_proxy(
        "socks5://private-user:private-password@1.2.3.4:1080",
        pool="res",
        max_identities=2,
    )
    identities = []
    for index in range(2):
        registered = await space.register(
            service=service.name,
            external_ref=f"proxy-pg-{index}",
            credentials={"phone": f"+2000{index}"},
            session={"auth_key": f"key-{index}", "dc_id": 2},
            lifecycle="ready",
        )
        identities.append(registered.identity_id)
    first = await space.pool("workers", service=service.name).acquire(among=[identities[0]])
    second = await space.pool("workers", service=service.name).acquire(among=[identities[1]])
    await second.release()

    overview = await space.proxy_overview(filters=ProxyFilters(capacity="full"))
    detail = await space.proxy_detail(proxy_id)

    assert len(overview.items) == 1
    assert overview.items[0].bound_count == len(detail.bound_identities) == 2
    assert overview.items[0].in_lease_count == 1
    assert overview.items[0].masked_url == "socks5://1.2.3.4:1080"
    assert len(detail.history.items) == 2
    assert "private-user" not in repr((overview, detail))
    assert "private-password" not in repr((overview, detail))

    other = hub.space("proxy-pg-other", "Proxy PG other")
    foreign_id, _ = await other.add_proxy("http://9.9.9.9:80", pool="res")
    with pytest.raises(NotFound):
        await space.proxy_detail(foreign_id)
    await first.release()
