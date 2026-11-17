from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub import (
    IdentityHub,
    ImportRecord,
    OperationNotAllowed,
    RuntimeServiceCreate,
    Service,
    StaticSecretsProvider,
)

pytestmark = pytest.mark.pg


class Config(BaseModel):
    prefix: str = ""


class Template:
    name = "test/runtime-pg-v1"
    config_schema = Config

    def build(self, definition: object) -> Service:
        assert hasattr(definition, "config")
        return Service(
            name=definition.name,  # type: ignore[attr-defined]
            title=definition.title,  # type: ignore[attr-defined]
            url=definition.url,  # type: ignore[attr-defined]
            normalize_ref=lambda value: definition.config["prefix"] + value,  # type: ignore[attr-defined]
            normalize_ref_version=1,
        )


async def test_postgres_runtime_definition_is_shared_and_revision_aware(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    secrets = StaticSecretsProvider({"k": bytes(range(32))}, "k")
    first = IdentityHub(sessionmaker, secrets, service_templates=[Template()])
    second = IdentityHub(sessionmaker, secrets, service_templates=[Template()])
    await first.start()
    await second.start()
    space = await second.spaces.ensure("runtime-pg")

    definition = await first.service_definitions.create(
        RuntimeServiceCreate(
            name="dynamic_pg",
            title="Dynamic PG",
            url="https://example.com",
            template="test/runtime-pg-v1",
            config={"prefix": "+"},
        ),
        actor="test",
    )
    report = await space.imports.run(
        service=definition.name,
        records=[ImportRecord("1")],
        actor="test",
    )
    identity = await space.identities.get(report.created[0])
    assert identity.normalized_ref == "+1"

    updated = await first.service_definitions.update(
        definition.name,
        title="Dynamic PG 2",
        expected_revision=definition.revision,
        actor="test",
    )
    assert (await second.resolve_service(updated.name)).title == "Dynamic PG 2"

    disabled = await first.service_definitions.disable(
        updated.name,
        expected_revision=updated.revision,
        actor="test",
        reason="supplier removed site",
    )
    with pytest.raises(OperationNotAllowed):
        await space.imports.run(
            service=disabled.name,
            records=[ImportRecord("2")],
            actor="test",
        )
