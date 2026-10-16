"""S5.2: крупная пачка и предупреждения открытого хранения на MemoryStore."""

from __future__ import annotations

import logging

import pytest

from identity_hub import IdentityCreate, IdentityHub, StaticSecretsProvider
from identity_hub.hub import PLAINTEXT_SECRET_WARNING
from identity_hub.testing import MemoryStore
from tests.conftest import TEST_KEYS
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.unit


def _hub(store: MemoryStore, *, storage: str = "encrypted") -> IdentityHub:
    return IdentityHub(
        store=store,
        secrets=StaticSecretsProvider(TEST_KEYS, "k1"),
        services=[FAKE_TG],
        secret_storage=storage,
    )


async def test_convert_one_thousand_rows_in_batches() -> None:
    hub = _hub(MemoryStore())
    await hub.start()
    space = hub.space("bulk-convert", create=True)
    specs = [
        IdentityCreate(
            service="fake_tg",
            external_ref=f"+bulk-{number}",
            credentials={"phone": f"+bulk-{number}", "password": "p"},
            session={"auth_key": f"k-{number}"},
        )
        for number in range(500)
    ]
    result = await space.identities.create_many(specs, actor="test")
    assert len(result.created) == 500

    plaintext = await hub.doctor.convert_secrets(to="plaintext", batch=73)
    assert plaintext.total == 1000
    assert plaintext.counts == {
        "credentials": 500,
        "sessions": 500,
        "challenges": 0,
        "verifications": 0,
        "proxies": 0,
        "webhooks": 0,
        "notification_channels": 0,
        "profiles": 0,
        "import_items": 0,
    }
    encrypted = await hub.doctor.convert_secrets(to="encrypted", batch=61)
    assert encrypted.total == 1000
    assert await hub.doctor.plaintext_secrets() == {}


async def test_start_warns_for_mode_and_existing_plaintext(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MemoryStore()
    plaintext = _hub(store, storage="plaintext")
    with caplog.at_level(logging.WARNING, logger="identity_hub"):
        await plaintext.start()
    assert PLAINTEXT_SECRET_WARNING in caplog.text

    space = plaintext.space("warning", create=True)
    await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+warning",
            credentials={"phone": "+warning", "password": "p"},
        ),
        actor="test",
    )
    caplog.clear()
    encrypted = _hub(store)
    with caplog.at_level(logging.WARNING, logger="identity_hub"):
        await encrypted.start()
    assert PLAINTEXT_SECRET_WARNING in caplog.text
