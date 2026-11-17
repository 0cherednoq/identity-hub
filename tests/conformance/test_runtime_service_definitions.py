from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import BaseModel

from identity_hub import (
    IdempotencyConflict,
    IdentityHub,
    ImportRecord,
    OperationNotAllowed,
    RuntimeServiceCreate,
    Service,
    StaticSecretsProvider,
    ValidationFailed,
)
from identity_hub.events import Event
from identity_hub.testing import MemoryStore

pytestmark = pytest.mark.conformance


class Config(BaseModel):
    prefix: str = ""


class Template:
    name = "test/v1"
    config_schema = Config

    def build(self, definition: object) -> Service:
        assert hasattr(definition, "config")
        return Service(
            name=definition.name,  # type: ignore[attr-defined]
            title=definition.title,  # type: ignore[attr-defined]
            url=definition.url,  # type: ignore[attr-defined]
            normalize_ref=lambda value: definition.config["prefix"] + value.strip(),  # type: ignore[attr-defined]
            normalize_ref_version=1,
        )


def _secrets() -> StaticSecretsProvider:
    return StaticSecretsProvider({"k": bytes(range(32))}, "k")


async def test_runtime_definition_is_visible_to_an_already_started_hub() -> None:
    store = MemoryStore()
    first = IdentityHub(
        store=store, secrets=_secrets(), service_templates=[Template()]
    )
    second = IdentityHub(
        store=store, secrets=_secrets(), service_templates=[Template()]
    )
    background = IdentityHub(
        store=store, secrets=_secrets(), service_templates=[Template()]
    )
    await first.start()
    await second.start()
    await background.start()
    space = await second.spaces.ensure("runtime")

    definition, created = await first.service_definitions.ensure(
        RuntimeServiceCreate(
            name="dynamic",
            title="Dynamic",
            url="https://example.com",
            template="test/v1",
            config={"prefix": "+"},
        ),
        actor="test",
        idempotency_key="supplier:1",
    )
    report = await space.imports.run(
        service=definition.name,
        records=[ImportRecord("1")],
        on_existing="merge",
        actor="test",
    )
    identity = await space.identities.get(report.created[0])

    assert created is True
    assert report.failed == []
    assert identity.normalized_ref == "+1"
    assert second.service("dynamic").name == "dynamic"
    assert "dynamic" in {service.name for service in await background._active_services()}


async def test_idempotency_revision_and_disable_semantics() -> None:
    store = MemoryStore()
    hub = IdentityHub(
        store=store, secrets=_secrets(), service_templates=[Template()]
    )
    await hub.start()
    spec = RuntimeServiceCreate(
        name="dynamic", title="Dynamic", url=None, template="test/v1", config={}
    )
    definition, created = await hub.service_definitions.ensure(
        spec, actor="test", idempotency_key="supplier:1"
    )
    replay, replay_created = await hub.service_definitions.ensure(
        spec, actor="test", idempotency_key="supplier:1"
    )
    assert (replay, replay_created) == (definition, False)
    assert created is True

    with pytest.raises(IdempotencyConflict):
        await hub.service_definitions.ensure(
            replace(spec, title="Other"), actor="test", idempotency_key="supplier:1"
        )

    updated = await hub.service_definitions.update(
        definition.name,
        title="Dynamic 2",
        expected_revision=definition.revision,
        actor="test",
    )
    assert updated.revision == 2
    service = await hub.resolve_service(updated.name)
    space = await hub.spaces.ensure("runtime-disable")
    pool = space.pools.define("runtime", service=service)
    disabled = await hub.service_definitions.disable(
        updated.name, expected_revision=updated.revision, actor="test"
    )
    with pytest.raises(OperationNotAllowed):
        await hub.resolve_service(disabled.name)
    with pytest.raises(OperationNotAllowed):
        await hub.resolve_service(service)
    with pytest.raises(OperationNotAllowed):
        await pool.acquire()

    enabled = await hub.service_definitions.enable(
        disabled.name, expected_revision=disabled.revision, actor="test"
    )
    seen: list[Event] = []

    async def collect(events: list[Event]) -> None:
        seen.extend(events)

    assert await store.process_events("runtime-audit", collect, limit=20) == 4
    assert [event.type for event in seen] == [
        "service.definition.created",
        "service.definition.updated",
        "service.definition.disabled",
        "service.definition.enabled",
    ]
    assert all(event.space == "__global__" for event in seen)
    assert all("config" not in event.data for event in seen)
    assert enabled.revision == 4


async def test_template_registry_is_frozen_after_start() -> None:
    hub = IdentityHub(
        store=MemoryStore(), secrets=_secrets(), service_templates=[Template()]
    )
    await hub.start()
    with pytest.raises(ValidationFailed):
        hub.register_service_template(Template())
