"""PA1.3: импорт не обходит strict capacity и явно отделяет доверие от проверки."""

from dataclasses import replace

import pytest
from tests.fixtures.api import register

from identity_hub import ImportRecord, ProxyBindingPolicy, ProxyPolicy, SpaceHandle, ValidationFailed
from identity_hub.importing import ImportRunner
from identity_hub.proxy_allocator import reserve
from identity_hub.read.events import EventFilters

pytestmark = pytest.mark.conformance


async def _setup(space: SpaceHandle):
    service = replace(
        space.hub.service("fake_tg"),
        name="strict_import",
        proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy()),
    )
    space.hub.register_service(service)
    await space.hub.start()
    proxy = await space.proxies.create("http://u:p@127.0.0.1:8930", group="g", max_identities=1)
    await space.proxies.configure_group(
        "g", capacity_accounting="allocations", actor="admin", reason="switch"
    )
    return service, proxy


@pytest.mark.parametrize(
    "options",
    [
        {"proxy_binding": "bad"},
        {"proxy_binding": []},
        {"proxy_binding": "trusted"},
        {"proxy_binding": "trusted", "proxy_binding_reason": ""},
        {"proxy_binding": "policy", "proxy_binding_reason": "ignored"},
    ],
)
async def test_trust_options_validate_before_consuming_records(space: SpaceHandle, options) -> None:
    def records():
        raise AssertionError("invalid command consumed records")
        yield ImportRecord("a")

    with pytest.raises(ValidationFailed):
        await space.imports.run(service="fake_tg", records=records(), actor="admin", **options)


async def test_trusted_import_uses_allocator_and_audits_reason(space: SpaceHandle) -> None:
    service, proxy = await _setup(space)
    report = await space.imports.run(
        service=service,
        records=[ImportRecord(str(n), proxy="http://u:p@127.0.0.1:8930") for n in range(3)],
        actor="admin",
        proxy_binding="trusted",
        proxy_binding_reason="verified before migration",
    )
    assert len(report.created) == 1 and len(report.failed) == 2
    async with space._tx() as tx:
        allocations = await tx.proxy_allocations(proxy_id=proxy.id)
        assert len(allocations) == 1 and allocations[0].state == "bound"
        assert allocations[0].reason == "trusted_import"
        assert (await tx.get_proxy(proxy.id)).bound_count == 1
        for ref, _ in report.failed:
            assert (service.name, ref) not in await tx.find_by_refs([(service.name, ref)])
    events = (await space.admin.event_page(filters=EventFilters(type="proxy.binding.changed"))).items
    assert len(events) == 1 and events[0].actor == "admin"
    assert events[0].data["reason"] == "trusted import: verified before migration"
    assert "http://" not in str(events[0].data)


async def test_import_respects_reservation_and_keeps_independent_records(space: SpaceHandle) -> None:
    _, proxy = await _setup(space)
    account = await register(
        space, service="fake_tg", external_ref="owner", lifecycle="ready", session={"auth_key": "k"}
    )
    # Конфигурация группы была изменена до появления этой аренды.
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[account.identity_id])
    async with space._tx() as tx:
        await reserve(tx, account.identity_id, "g", owner_lease_id=lease.lease_id)
    report = await space.imports.run(
        service="fake_tg",
        records=[
            ImportRecord("full", proxy="http://u:p@127.0.0.1:8930"),
            ImportRecord("dependent", links={"recovery": "full"}),
            ImportRecord("independent"),
        ],
        actor="admin",
    )
    assert len(report.created) == 1 and {ref for ref, _ in report.failed} == {"full", "dependent"}
    async with space._tx() as tx:
        assert (await tx.get_proxy(proxy.id)).reserved_count == 1
        assert (await tx.get_proxy(proxy.id)).bound_count == 0
    await lease.release()


async def test_import_uses_actual_deduplicated_group(space: SpaceHandle) -> None:
    _, proxy = await _setup(space)
    # fake_tg использует import, но URL уже принадлежит строгой g.
    report = await space.imports.run(
        service="fake_tg",
        records=[ImportRecord("a", proxy="http://u:p@127.0.0.1:8930")],
        actor="admin",
    )
    assert len(report.created) == 1 and not report.failed
    async with space._tx() as tx:
        binding = (await tx.proxy_bindings(identity_id=report.created[0]))[0]
        assert binding.proxy_id == proxy.id and binding.allocation_id is not None
        assert (await tx.proxy_allocation(binding.allocation_id)).state == "bound"


async def test_trusted_import_hook_rollback_returns_capacity(space: SpaceHandle) -> None:
    service, proxy = await _setup(space)

    async def reject(_session, _created):
        raise RuntimeError("rollback")

    report = await space.imports.run(
        service=service,
        records=[ImportRecord("a", proxy="http://u:p@127.0.0.1:8930")],
        actor="admin",
        proxy_binding="trusted",
        proxy_binding_reason="migration",
        on_batch=reject,
    )
    assert not report.created and len(report.failed) == 1
    async with space._tx() as tx:
        assert await tx.proxy_allocations(proxy_id=proxy.id) == []
        assert (await tx.get_proxy(proxy.id)).bound_count == 0


async def test_staged_import_preserves_trust_decision(space: SpaceHandle) -> None:
    service, proxy = await _setup(space)
    run_id = await space.imports.stage(
        service=service,
        records=[ImportRecord("a", proxy="http://u:p@127.0.0.1:8930")],
        source="migration",
        actor="admin",
        proxy_binding="trusted",
        proxy_binding_reason="previously checked",
    )
    runner = ImportRunner(space.hub)
    assert await runner.run_once() == 1
    assert await runner.run_once() == 0
    run = await space.imports.get(run_id)
    assert (run.created, run.failed, run.state) == (1, 0, "succeeded")
    async with space._tx() as tx:
        assert len(await tx.proxy_allocations(proxy_id=proxy.id)) == 1
        assert (await tx.get_proxy(proxy.id)).bound_count == 1
