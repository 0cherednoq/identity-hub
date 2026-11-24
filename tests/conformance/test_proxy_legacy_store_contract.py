"""PA5/R03/R15: сохранённые legacy helper соблюдают режим и общий лимит."""

import pytest
from tests.fixtures.api import register

from identity_hub import ValidationFailed

pytestmark = pytest.mark.conformance


async def test_single_insert_and_raw_legacy_unbind_preserve_mode_contract(space):
    candidate = space._prepare_proxy("http://127.0.0.1:9088", "legacy", None, None, None, None, None, 1, None)
    async with space._tx() as tx:
        proxy, created = await tx.insert_proxy(candidate)
        assert created
        assert await tx.insert_proxy(candidate) == (proxy, False)
    identity = (await register(space, service="fake_tg", external_ref="raw-helper")).identity_id
    await space.proxies.rebind(identity, proxy, actor="admin", reason="legacy binding")
    async with space._tx() as tx:
        assert await tx.unbind_identity(identity) == proxy
        assert await tx.unbind_identity(identity) is None
        assert (await tx.get_proxy(proxy)).bound_count == 0
    await space.proxies.configure_group(
        "legacy", capacity_accounting="allocations", actor="admin", reason="strict"
    )
    await space.proxies.rebind(identity, proxy, actor="admin", reason="trusted strict binding")
    with pytest.raises(ValidationFailed, match="managed proxy"):
        async with space._tx() as tx:
            await tx.unbind_identity(identity)
    assert (await space.admin.proxy_detail(proxy)).proxy.bound_count == 1
