"""MemoryStore через публичный IdentityHub, без SQL и Docker."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import BaseModel, SecretStr

from identity_hub._ids import new_id
from identity_hub.clock import FakeClock
from identity_hub.crypto import Envelope, StaticSecretsProvider, associated_data
from identity_hub.enums import Severity
from identity_hub.events import EventDraft
from identity_hub.hub import IdentityHub
from identity_hub.testing import MemoryStore
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG, TgCredentials

pytestmark = pytest.mark.anyio
TEST_KEYS = {"k1": b"\x33" * 32, "k2": b"\x44" * 32}


class WorkerData(BaseModel):
    attempts: int


class FakeTelegramTransport:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(
        self,
        bot_token: SecretStr,
        chat_id: int | str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        self.sent.append(
            {
                "token": bot_token.get_secret_value(),
                "chat_id": chat_id,
                "thread_id": thread_id,
                "text": text,
            }
        )


@pytest.fixture
async def memory_hub() -> IdentityHub:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    hub = IdentityHub(
        secrets=StaticSecretsProvider(TEST_KEYS, "k1"),
        services=[FAKE_TG, FAKE_EXCHANGE],
        store=MemoryStore(clock),
        clock=clock,
        strict_events=True,
    )
    await hub.start()
    return hub


async def test_public_register_mutate_search_and_lease(memory_hub: IdentityHub) -> None:
    space = memory_hub.space("memory", "Memory")
    registered = await space.register(
        service="fake_tg",
        external_ref="+380501112233",
        credentials=TgCredentials(phone="+380501112233", password=SecretStr("hunter2")),
        session={"auth_key": "AAAA", "dc_id": 4},
        lifecycle="ready",
        profile={"first_name": "Olena", "country": "UA"},
        labels={"farm": 3},
        tags=["parser"],
    )
    await space.set_labels(registered.identity_id, {"level": 2}, merge=True)
    await space.set_note(registered.identity_id, "Проверить в пятницу")

    page = await space.search("service:fake_tg olena")
    assert [row.id for row in page.items] == [registered.identity_id]
    assert page.items[0].labels == {"farm": 3, "level": 2}

    async with space.pool("parsers", service="fake_tg", holder="test").lease() as lease:
        assert lease.identity_id == registered.identity_id
        async with lease.operation("read_channel"):
            pass

    card = await space.identity_card(registered.identity_id)
    stats = await space.lease_stats(
        registered.identity_id, datetime(2026, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
    )
    assert card.identity.note == "Проверить в пятницу"
    assert card.lease is None
    assert stats.count == 1


async def test_public_links_and_identity_data(memory_hub: IdentityHub) -> None:
    space = memory_hub.space("memory")
    mail = await space.register(service="fake_exchange", external_ref="mail@example.test")
    account = await space.register(
        service="fake_tg",
        external_ref="+1",
        links={"recovery_email": mail.identity_id},
    )

    await space.save_data(
        account.identity_id,
        "worker",
        WorkerData(attempts=1),
        expected_version=None,
    )
    stored = await space.load_data(account.identity_id, "worker", WorkerData)
    links = await space.links_of(account.identity_id)

    assert stored is not None and stored.value == WorkerData(attempts=1)
    assert [(row.role, row.identity_id) for row in links.outgoing] == [("recovery_email", mail.identity_id)]
    assert links.incoming == []


@respx.mock
async def test_public_webhook_dispatch(memory_hub: IdentityHub) -> None:
    space = memory_hub.space("memory")
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/memory",
        secret=SecretStr("memory-secret"),
        event_types=["identity.*"],
        filter={"labels": {"farm": 3}},
    )
    route = respx.post(endpoint.url).mock(return_value=httpx.Response(204))
    await space.register(service="fake_tg", external_ref="+webhook", labels={"farm": 3})

    await memory_hub.dispatcher.drain()

    assert route.call_count == 1
    deliveries = await space.webhooks.deliveries(endpoint.id)
    assert deliveries[0].status == "succeeded"
    assert deliveries[0].attempt == 1


async def test_public_notification_dispatch(memory_hub: IdentityHub) -> None:
    transport = FakeTelegramTransport()
    memory_hub.notification_dispatcher.transport = transport
    space = memory_hub.space("memory")
    channel = await space.notifications.create_telegram_channel(
        "Ops", bot_token=SecretStr("123456:memory-token"), chat_id=-100
    )
    await space.notifications.create_rule(
        name="Баны",
        channel_id=channel.id,
        event_types=["identity.standing.changed"],
        filter={"data.to": ["banned"]},
        min_severity=Severity.WARNING,
        created_by="test",
    )
    identity = await space.register(service="fake_tg", external_ref="+notify")
    async with space._tx() as transaction:
        await space._emit(
            transaction,
            EventDraft(
                "identity.standing.changed",
                "test",
                severity=Severity.WARNING,
                service="fake_tg",
                identity_id=identity.identity_id,
                data={"from": "good", "to": "banned"},
            ),
        )

    await memory_hub.dispatcher.drain()

    assert len(transport.sent) == 1
    assert transport.sent[0]["token"] == "123456:memory-token"
    assert "identity.standing.changed" in transport.sent[0]["text"]


async def test_public_key_rotation_covers_all_secret_kinds(memory_hub: IdentityHub) -> None:
    secret_service = replace(FAKE_TG, name="memory_secret_tg", profile_encrypted=True)
    memory_hub.register_service(secret_service)
    await memory_hub.start()
    space = memory_hub.space("rotation")
    registered = await space.register(
        service="memory_secret_tg",
        external_ref="+rotate",
        credentials=TgCredentials(phone="+rotate", password=SecretStr("password")),
        session={"auth_key": "auth-key", "dc_id": 2},
        profile={"first_name": "Ada", "country": "GB"},
    )
    identity_id = registered.identity_id
    await space.add_proxy("http://login:pass@127.0.0.1:8080", pool="rotate")
    await space.webhooks.create(
        url="https://hooks.example.test/rotate",
        secret=SecretStr("webhook-secret"),
        event_types=["*"],
    )
    await space.notifications.create_telegram_channel(
        "rotate", bot_token=SecretStr("123456:bot-secret"), chat_id=1
    )

    challenge_id = new_id()
    challenge = memory_hub.envelope.seal(
        b"{}", associated_data("ih_challenges", identity_id, str(challenge_id))
    )
    evidence = memory_hub.envelope.seal(b"{}", associated_data("ih_verifications", identity_id, "phone"))
    async with space._tx() as transaction:
        assert await transaction.open_challenge(challenge_id, identity_id, "sms", "***", challenge, None)
        await transaction.save_verification(identity_id, "phone", {"attempts": 0}, evidence)

    memory_hub.envelope = Envelope(StaticSecretsProvider(TEST_KEYS, "k2"))
    report = await memory_hub.doctor.rotate_key(limit=100)

    assert report.total == 8
    assert set(report.counts) == {
        "credentials",
        "sessions",
        "challenges",
        "verifications",
        "proxies",
        "webhooks",
        "notification_channels",
        "profiles",
    }
