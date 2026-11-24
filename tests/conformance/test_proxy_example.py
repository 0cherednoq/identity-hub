"""PA5.2: пример потребителя выполняется только через публичный API на обоих Store."""

import pytest
from examples.proxy_assignment import (
    FakeSite,
    Verdict,
    account,
    configure_proxy_group,
    demonstrate,
    manual_check,
    site_service,
)

from identity_hub import ProxyAssignmentConflict

pytestmark = pytest.mark.conformance


async def test_public_proxy_assignment_example(hub_under_test):
    client = FakeSite()
    hub_under_test.register_service(site_service(client))
    await hub_under_test.start()
    result = await demonstrate(hub_under_test, client)
    assert result == {
        "login": "succeeded",
        "manual": "bound",
        "unknown": "failed",
        "challenge": "challenged",
        "later_check": "invalid",
        "bound": 1,
        "temporary_slots": 0,
        "provider_used_proxy": True,
    }
    assert "demo-password" not in repr(result) and "candidate-session" not in repr(result)


@pytest.mark.parametrize("replace_binding", [False, True])
async def test_manual_invalid_removes_only_checked_generation(hub_under_test, replace_binding):
    hub_under_test.register_service(site_service(FakeSite()))
    await hub_under_test.start()
    space = hub_under_test.space("late-manual-example", create=True)
    original = await configure_proxy_group(space)
    replacement = await space.proxies.create("http://127.0.0.1:9086", group="site-egress")
    identity = await account(space, "late")
    pool = space.pools.define("checks", service="proxy_example_site")

    async def valid(_lease):
        return Verdict.VALID

    await manual_check(space, pool, identity, valid)

    async def checker(_lease):
        if replace_binding:
            await space.proxies.rebind(identity, replacement.id, actor="operator", reason="new assignment")
        return Verdict.INVALID

    if replace_binding:
        with pytest.raises(ProxyAssignmentConflict):
            await manual_check(space, pool, identity, checker)
    else:
        await manual_check(space, pool, identity, checker)
    detail = (await space.admin.proxy_detail(replacement.id)).proxy
    assert detail.bound_count == int(replace_binding)
    assert (await space.admin.proxy_detail(original)).proxy.occupied_count == 0
    assert (await space.identities.get(identity)).auth == "authenticated"
