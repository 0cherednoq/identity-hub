"""PA0.2: правила доступны пользователю без Any и внутренних импортов."""

from typing import assert_type
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from identity_hub import (
    AllOf,
    AnyOf,
    BatchResult,
    ImportRecord,
    ImportReport,
    Lease,
    ProxyAssignment,
    ProxyBindingPolicy,
    ProxyGroup,
    ProxyPolicy,
    RegionIn,
    Service,
    SpaceHandle,
)

auth = RegionIn("auth", states={"authenticated"})
standing = AnyOf(RegionIn("standing", states={"good"}), RegionIn("standing", states={"limited"}))
policy = ProxyPolicy(
    group="site",
    binding=ProxyBindingPolicy(
        bind_when=AllOf(auth, standing), unbind_when=RegionIn("standing", states={"banned"})
    ),
)
assert_type(auth.states, frozenset[str])
assert_type(standing.matches({("standing", ""): "good"}), bool)
assert_type(Service(name="site", title="Site", proxy_policy=policy).proxy_policy, ProxyPolicy | None)


async def proxy_group_api(space: SpaceHandle) -> None:
    proxies = await space.proxies.list()
    assert_type(proxies.items[0].occupied_count, int | None)
    card = (await space.admin.proxy_detail(proxies.items[0].id)).proxy
    assert_type(card.occupied_count, int)
    assert_type(card.cleanup_pending_count, int)
    assert_type(card.capacity_accounting, str)
    assert_type(await space.maintenance.cleanup_proxy_allocations(limit=10), int)
    assert_type(await space.proxies.get_group("site"), ProxyGroup)
    assert_type(
        await space.proxies.configure_group(
            "site", capacity_accounting="allocations", actor="ops", reason="enable accounting"
        ),
        ProxyGroup,
    )


async def proxy_generation_api(space: SpaceHandle, identity: UUID, proxy: UUID, binding: UUID) -> None:
    assert_type(
        await space.proxies.rebind(
            identity, proxy, expected_binding_id=binding, actor="ops", reason="trusted replacement"
        ),
        bool,
    )
    assert_type(
        await space.proxies.unbind(identity, expected_binding_id=binding, actor="ops", reason="remove"),
        bool,
    )


async def proxy_import_api(space: SpaceHandle) -> None:
    assert_type(
        await space.imports.run(
            service="site",
            records=[ImportRecord("a")],
            actor="ops",
            proxy_binding="trusted",
            proxy_binding_reason="migration",
        ),
        ImportReport,
    )
    assert_type(
        await space.imports.stage(
            service="site",
            records=[ImportRecord("a")],
            source="migration",
            actor="ops",
            proxy_binding="trusted",
            proxy_binding_reason="previously checked",
        ),
        UUID,
    )


def proxy_lease_snapshot(lease: Lease) -> None:
    assert_type(lease.proxy_allocation_id, UUID | None)
    assert_type(lease.proxy_assignment_state, str | None)


async def proxy_confirmation_api(lease: Lease, session: AsyncSession) -> None:
    assert_type(await lease.confirm_proxy(reason="accepted"), ProxyAssignment)
    assert_type(await lease.using(session).confirm_proxy(reason="accepted"), ProxyAssignment)


async def proxy_reconcile_api(space: SpaceHandle, identity: UUID, binding: UUID) -> None:
    assert_type(
        await space.proxies.reconcile_bindings(
            [identity],
            service="site",
            proxy_policy=policy,
            actor="operator",
            reason="new policy",
            expected_service_revision=1,
            expected_bindings={identity: binding},
            atomic=True,
        ),
        BatchResult[UUID],
    )
