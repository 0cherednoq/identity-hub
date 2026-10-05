"""IMP-09 (I3.2b): credentials при регистрации, импорте, в карточке и фабриках (D-217)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import BaseModel, SecretStr
from tests.fixtures.services import FAKE_TG

from identity_hub import (
    CredentialInput,
    IdentityCreate,
    IdentityHub,
    ImportRecord,
    SecretRef,
    SpaceHandle,
    ValidationFailed,
)

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]


class ApiKey(BaseModel):
    secret: SecretStr


async def _service(hub: IdentityHub) -> None:
    hub.register_service(replace(FAKE_TG, name="fake_keys", credential_kinds={"api_key": ApiKey}))
    await hub.start()


async def test_register_lease_rotate_reveal(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _service(hub_under_test)
    created = await space.identities.create(
        IdentityCreate(
            service="fake_keys",
            external_ref="+1",
            lifecycle="ready",
            credentials={"phone": "+1", "password": "p"},
            session={"auth_key": "k"},
            credential_records=[
                CredentialInput("api_key", {"secret": "s1"}, name="trading", public_value={"key_id": "T1"}),
            ],
        ),
        actor="test",
    )

    lease = await space.pools.define("work", service="fake_keys", holder="w").acquire()
    key = await lease.credential("api_key", "trading")
    assert key is not None and key.secret.get_secret_value() == "s1"  # type: ignore[attr-defined]
    await lease.release()

    await space.secrets.put_credential(created.id, "api_key", {"secret": "s2"}, name="trading", actor="ops")
    revealed = await space.secrets.reveal(
        created.id, SecretRef.credential("api_key", "trading"), actor="admin", reason="проверка"
    )
    assert revealed == {"secret": "s2"}

    card = await space.secrets.describe(created.id)
    assert sorted((c.kind, c.name, c.public_value) for c in card.credential_records) == [
        ("api_key", "trading", None),  # ротация заменила открытую часть вместе с секретом
        ("default", "default", None),
    ]
    assert "s2" not in repr(card)


async def test_create_rejects_duplicate_records(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    await _service(hub_under_test)
    with pytest.raises(ValidationFailed):
        await space.identities.create(
            IdentityCreate(
                service="fake_keys",
                external_ref="+2",
                credentials={"phone": "+2", "password": "p"},
                credential_records=[CredentialInput("default", {"phone": "+2", "password": "x"})],
            ),
            actor="test",
        )
    with pytest.raises(ValidationFailed):
        await space.identities.create(
            IdentityCreate(
                service="fake_keys",
                external_ref="+3",
                credential_records=[
                    CredentialInput("api_key", {"secret": "a"}, name="k"),
                    CredentialInput("api_key", {"secret": "b"}, name="k"),
                ],
            ),
            actor="test",
        )


@pytest.mark.parametrize("staged", [False, True], ids=["run", "stage"])
async def test_import_writes_records_once(
    hub_under_test: IdentityHub, space: SpaceHandle, staged: bool
) -> None:
    await _service(hub_under_test)
    records = [
        ImportRecord(
            external_ref="ex-1",
            credentials={"phone": "+9", "password": "p"},
            credential_records=[CredentialInput("api_key", {"secret": "imp"}, name="main")],
        )
    ]

    async def run() -> None:
        if staged:
            run_id = await space.imports.stage(
                service="fake_keys", records=records, source="keys.jsonl", actor="test"
            )
            assert await hub_under_test._import_runner.run_once() == 1
            assert (await space.imports.get(run_id)).state == "succeeded"
        else:
            await space.imports.run(service="fake_keys", records=records, actor="test")

    await run()
    await run()  # повтор: существующий аккаунт не трогается, дубликатов нет

    identity = await space.identities.get_by_ref("fake_keys", "ex-1")
    card = await space.secrets.describe(identity.id)
    assert sorted((c.kind, c.name) for c in card.credential_records) == [
        ("api_key", "main"),
        ("default", "default"),
    ]
    value = await space.secrets.reveal(
        identity.id, SecretRef.credential("api_key", "main"), actor="admin", reason="проверка"
    )
    assert value == {"secret": "imp"}


async def test_factory_accepts_records(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    from identity_hub.testing import make_identity

    await _service(hub_under_test)
    identity_id = await make_identity(
        space,
        service="fake_keys",
        authenticated=False,
        credential_records=[CredentialInput("api_key", {"secret": "f"}, name="x")],
    )
    card = await space.secrets.describe(identity_id)
    assert ("api_key", "x") in {(c.kind, c.name) for c in card.credential_records}
