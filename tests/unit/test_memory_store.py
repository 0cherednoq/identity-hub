"""MemoryStore через публичный IdentityHub, без SQL и Docker."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import BaseModel, SecretStr

from identity_hub._ids import new_id
from identity_hub.clock import FakeClock
from identity_hub.crypto import Envelope, StaticSecretsProvider, associated_data
from identity_hub.domain.regions import Command, Observation
from identity_hub.enums import Availability, Severity, Standing
from identity_hub.errors import NotFound, ValidationFailed
from identity_hub.events import EventDraft
from identity_hub.hub import IdentityHub
from identity_hub.read.background import classify_health
from identity_hub.read.proxies import ProxyFilters
from identity_hub.read.search import AccountSearchOptions
from identity_hub.service import ProxyPolicy
from identity_hub.state import Override
from identity_hub.testing import MemoryStore
from tests.fixtures.api import add_proxy, register
from tests.fixtures.services import FAKE_EXCHANGE, FAKE_TG, KycEvidence, TgCredentials
from tests.fixtures.shared_lease import (
    SHARED_TG,
    check_parallel_shared_leases_keep_writing,
    check_shared_rejects_idempotency_key,
    check_shared_rotates_least_recently_leased,
)

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
    space = memory_hub.space("memory", "Memory", create=True)
    registered = await register(
        space,
        service="fake_tg",
        external_ref="+380501112233",
        credentials=TgCredentials(phone="+380501112233", password=SecretStr("hunter2")),
        session={"auth_key": "AAAA", "dc_id": 4},
        lifecycle="ready",
        profile={"first_name": "Olena", "country": "UA"},
        labels={"farm": 3},
        tags=["parser"],
    )
    await space.identities.update(registered.identity_id, labels={"level": 2})
    await space.identities.set_note(registered.identity_id, "Проверить в пятницу")

    page = await space.identities.search("service:fake_tg olena")
    assert [row.id for row in page.items] == [registered.identity_id]
    assert page.items[0].labels == {"farm": 3, "level": 2}

    async with space.pools.define("parsers", service="fake_tg", holder="test").lease() as lease:
        assert lease.identity_id == registered.identity_id
        async with lease.operation("read_channel"):
            pass

    card = await space.identities.card(registered.identity_id)
    stats = await space.admin.lease_stats(
        registered.identity_id, datetime(2026, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
    )
    assert card.identity.note == "Проверить в пятницу"
    assert card.lease is None
    assert stats.count == 1


async def test_service_catalog_is_deterministic_safe_and_scoped(memory_hub: IdentityHub) -> None:
    visible = memory_hub.space("catalog-visible", "Catalog visible", create=True)
    hidden = memory_hub.space("catalog-hidden", "Catalog hidden", create=True)
    for space, suffix in ((visible, "visible"), (hidden, "hidden")):
        await register(
            space,
            service="fake_tg",
            external_ref=f"catalog-{suffix}",
            credentials={"phone": f"+100{suffix}"},
            session={"auth_key": f"key-{suffix}", "dc_id": 2},
        )

    catalog = await memory_hub.service_catalog({"catalog-visible"})
    assert [service.name for service in catalog] == ["fake_exchange", "fake_tg"]
    telegram = next(service for service in catalog if service.name == "fake_tg")
    assert telegram.total == 1
    assert [(item.space_slug, item.count) for item in telegram.spaces] == [("catalog-visible", 1)]
    credentials = next(schema for schema in telegram.schemas if schema.category == "credentials")
    password = next(field for field in credentials.fields if field.name == "password")
    assert password.kind == "secret" and password.sensitive
    profile = next(schema for schema in telegram.schemas if schema.category == "profile")
    assert next(field for field in profile.fields if field.name == "country").filterable
    assert [operation.kind for operation in telegram.operations] == sorted(
        operation.kind for operation in telegram.operations
    )
    exchange = next(service for service in catalog if service.name == "fake_exchange")
    assert next(item for item in exchange.verifications if item.name == "kyc_id").evidence_required
    rendered = json.dumps([asdict(item) for item in catalog], default=str)
    assert "SecretStr" not in rendered
    assert "KycEvidence" not in rendered
    assert "function" not in rendered
    assert "catalog-hidden" not in rendered


async def test_background_health_distinguishes_all_states(memory_hub: IdentityHub) -> None:
    now = memory_hub.clock.now()
    assert classify_health(heartbeat_at=now, now=now, delayed_after=10, stopped_after=60) == "healthy"
    assert (
        classify_health(heartbeat_at=now - timedelta(seconds=20), now=now, delayed_after=10, stopped_after=60)
        == "delayed"
    )
    assert (
        classify_health(heartbeat_at=now, now=now, delayed_after=10, stopped_after=60, retrying=True)
        == "retrying"
    )
    assert classify_health(heartbeat_at=None, now=now, delayed_after=10, stopped_after=60) == "stopped"

    before = await memory_hub.background_snapshot({"catalog-visible"})
    assert all(item.status != "healthy" for item in before.processes)
    await memory_hub.dispatcher.run_once()
    after = await memory_hub.background_snapshot({"catalog-visible"})
    assert next(item for item in after.processes if item.name == "webhooks").status == "healthy"
    stale = next(item for item in after.processes if item.name == "stale-probes")
    assert stale.status == "stopped" and not stale.heartbeat_available


async def test_lease_dashboard_clips_boundaries_and_uses_stable_cursor(
    memory_hub: IdentityHub,
) -> None:
    clock = cast(FakeClock, memory_hub.clock)
    boundary = memory_hub.space("lease-boundary", "Lease boundary", create=True)
    await register(
        boundary,
        service="fake_tg",
        external_ref="boundary",
        lifecycle="ready",
        session={"auth_key": "fake", "dc_id": 1},
    )
    async with boundary.pools.define("workers", service="fake_tg", holder="worker-a").lease():
        clock.advance(timedelta(minutes=20))
    dashboard = await boundary.admin.lease_dashboard(group_by="holder")
    quarter = dashboard.windows[0]
    assert quarter.acquired == 0
    assert quarter.completed == 1
    assert quarter.leased_seconds == 15 * 60
    assert quarter.utilization == 1.0
    assert dashboard.slices[0].value == "worker-a"

    percentiles = memory_hub.space("lease-percentiles", "Lease percentiles", create=True)
    await register(
        percentiles,
        service="fake_tg",
        external_ref="percentiles",
        lifecycle="ready",
        session={"auth_key": "fake", "dc_id": 1},
    )
    pool = percentiles.pools.define("p95", service="fake_tg", holder="worker-p95")
    for seconds in range(1, 21):
        async with pool.lease():
            clock.advance(seconds)
    p95 = (await percentiles.admin.lease_dashboard()).windows[-1]
    assert p95.completed == 20
    assert p95.p95_seconds == pytest.approx(19.05)

    first = await percentiles.admin.lease_history_page(limit=7)
    second = await percentiles.admin.lease_history_page(limit=7, before=first.next_before)
    assert len(first.items) == len(second.items) == 7
    assert {item.lease_id for item in first.items}.isdisjoint(item.lease_id for item in second.items)


async def test_overview_handles_empty_single_large_and_allowed_spaces(memory_hub: IdentityHub) -> None:
    await memory_hub.space("empty", "Empty", create=True).ref()
    single = memory_hub.space("single", "Single", create=True)
    await register(
        single,
        service="fake_tg",
        external_ref="single",
        lifecycle="ready",
        auth_state="authenticated",
    )
    large = memory_hub.space("large", "Large", create=True)
    for index in range(250):
        await register(
            large,
            service="fake_tg",
            external_ref=f"large-{index:04d}",
            lifecycle="ready" if index % 5 else "provisioning",
            auth_state="authenticated",
        )

    matrix = await memory_hub.spaces_matrix({"empty", "single", "large"})
    assert [(item.space.slug, item.total) for item in matrix.spaces] == [
        ("empty", 0),
        ("large", 250),
        ("single", 1),
    ]
    assert next(item for item in matrix.spaces if item.space.slug == "large").eligible == 200
    assert [space.slug for space in await memory_hub.list_spaces({"single"})] == ["single"]


async def test_account_search_filters_and_equal_timestamp_keyset(memory_hub: IdentityHub) -> None:
    space = memory_hub.space("accounts", "Accounts", create=True)
    ids = []
    for index in range(5):
        result = await register(
            space,
            service="fake_tg",
            external_ref=f"account-{index}",
            lifecycle="ready" if index != 4 else "provisioning",
            auth_state="authenticated",
            tags=["selected"] if index == 0 else [],
        )
        ids.append(result.identity_id)
    await space.verifications.requirements.add(ids[0], "captcha")

    eligible = await space.admin.identity_page(options=AccountSearchOptions(eligible=True))
    assert len(eligible.items) == 4
    requirement = await space.admin.identity_page(options=AccountSearchOptions(requirement="captcha"))
    assert [item.id for item in requirement.items] == [ids[0]]
    assert requirement.items[0].requirements == ("captcha",)
    assert requirement.items[0].eligible is True
    assert requirement.items[0].blockers == ()

    seen: list[UUID] = []
    cursor = None
    while True:
        page = await space.admin.identity_page(limit=2, after=cursor, count=cursor is None)
        seen.extend(item.id for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == set(ids)

    sorted_refs: list[str] = []
    cursor = None
    while True:
        page = await space.admin.identity_page(
            options=AccountSearchOptions(sort_by="external_ref", sort_desc=False),
            limit=2,
            after=cursor,
        )
        sorted_refs.extend(item.external_ref for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert sorted_refs == [f"account-{index}" for index in range(5)]


async def test_public_links_and_identity_data(memory_hub: IdentityHub) -> None:
    space = memory_hub.space("memory", create=True)
    mail = await register(space, service="fake_exchange", external_ref="mail@example.test")
    account = await register(
        space,
        service="fake_tg",
        external_ref="+1",
        links={"recovery_email": mail.identity_id},
    )

    await space.data.save(
        account.identity_id,
        "worker",
        WorkerData(attempts=1),
        expected_version=None,
    )
    stored = await space.data.load(account.identity_id, "worker", WorkerData)
    links = await space.links.of(account.identity_id)

    assert stored is not None and stored.value == WorkerData(attempts=1)
    assert [(row.role, row.identity_id) for row in links.outgoing] == [("recovery_email", mail.identity_id)]
    assert links.incoming == []


@respx.mock
async def test_public_webhook_dispatch(memory_hub: IdentityHub) -> None:
    space = memory_hub.space("memory", create=True)
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/memory",
        secret=SecretStr("memory-secret"),
        event_types=["identity.*"],
        filter={"labels": {"farm": 3}},
    )
    route = respx.post("https://hooks.example.test/memory").mock(return_value=httpx.Response(204))
    await register(space, service="fake_tg", external_ref="+webhook", labels={"farm": 3})

    await memory_hub.dispatcher.drain()

    assert route.call_count == 1
    deliveries = await space.webhooks.deliveries(endpoint.id)
    assert deliveries[0].status == "succeeded"
    assert deliveries[0].attempt == 1


async def test_public_notification_dispatch(memory_hub: IdentityHub) -> None:
    transport = FakeTelegramTransport()
    memory_hub._notification_dispatcher.transport = transport
    space = memory_hub.space("memory", create=True)
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
    identity = await register(space, service="fake_tg", external_ref="+notify")
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


@respx.mock
async def test_direct_delivery_tests_do_not_touch_production_queues_or_cursors(
    memory_hub: IdentityHub,
) -> None:
    transport = FakeTelegramTransport()
    memory_hub._notification_dispatcher.transport = transport
    space = memory_hub.space("delivery-tests", create=True)
    channel = await space.notifications.create_telegram_channel(
        "Probe", bot_token=SecretStr("123456:test-token"), chat_id=-200
    )
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/probe",
        secret=SecretStr("signing-probe"),
        event_types=["identity.*"],
    )
    route = respx.post("https://hooks.example.test/probe").mock(return_value=httpx.Response(202))

    await space.notifications.send_test(channel.id, "direct probe")
    response_code = await space.webhooks.send_test(endpoint.id)

    store = cast(MemoryStore, memory_hub._store)
    assert response_code == 202
    assert route.call_count == 1
    assert transport.sent[0]["text"] == "direct probe"
    assert store._state.notifications == {}
    assert store._state.webhook_deliveries == {}
    assert store._state.cursors == {}


async def test_public_key_rotation_covers_all_secret_kinds(memory_hub: IdentityHub) -> None:
    secret_service = replace(FAKE_TG, name="memory_secret_tg", profile_encrypted=True)
    memory_hub.register_service(secret_service)
    await memory_hub.start()
    space = memory_hub.space("rotation", create=True)
    registered = await register(
        space,
        service="memory_secret_tg",
        external_ref="+rotate",
        credentials=TgCredentials(phone="+rotate", password=SecretStr("password")),
        session={"auth_key": "auth-key", "dc_id": 2},
        profile={"first_name": "Ada", "country": "GB"},
    )
    identity_id = registered.identity_id
    await add_proxy(space, "http://login:pass@127.0.0.1:8080", pool="rotate")
    await space.webhooks.create(
        url="https://hooks.example.test/rotate",
        secret=SecretStr("webhook-secret"),
        event_types=["*"],
    )
    await space.notifications.create_telegram_channel(
        "rotate", bot_token=SecretStr("123456:bot-secret"), chat_id=1
    )

    challenge_id = new_id()
    challenge = memory_hub._envelope.seal(
        b"{}", associated_data("ih_challenges", identity_id, str(challenge_id))
    )
    evidence = memory_hub._envelope.seal(b"{}", associated_data("ih_verifications", identity_id, "phone"))
    async with space._tx() as transaction:
        assert await transaction.open_challenge(challenge_id, identity_id, "sms", "***", challenge, None)
        await transaction.save_verification(identity_id, "phone", {"attempts": 0}, evidence)

    memory_hub._envelope = Envelope(StaticSecretsProvider(TEST_KEYS, "k2"))
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


async def test_human_queue_prioritizes_and_isolates_secret_free_attention(
    memory_hub: IdentityHub,
) -> None:
    space = memory_hub.space("attention", "Attention", create=True)
    other = memory_hub.space("other", "Other", create=True)
    clock = cast(FakeClock, memory_hub.clock)
    now = clock.now()

    async def exchange(external_ref: str) -> UUID:
        registered = await register(
            space,
            service="fake_exchange",
            external_ref=external_ref,
            credentials={"email": external_ref, "password": "secret-password"},
            session={"cookies": {"sid": "secret-cookie"}},
            lifecycle="ready",
        )
        return registered.identity_id

    challenge_id = await exchange("challenge@example.test")
    challenge_row_id = new_id()
    sealed = memory_hub._envelope.seal(
        b'{"answer":"never-return-this-secret"}',
        associated_data("ih_challenges", challenge_id, str(challenge_row_id)),
    )
    async with space._tx() as transaction:
        assert await transaction.open_challenge(
            challenge_row_id,
            challenge_id,
            "otp",
            "***42",
            sealed,
            now - timedelta(minutes=1),
        )

    requirement_id = await exchange("requirement@example.test")
    await space.verifications.requirements.add(
        requirement_id, "identity_document", deadline=now + timedelta(hours=2)
    )

    manual_id = await exchange("manual@example.test")
    lease = await space.pools.define("manual", service="fake_exchange").acquire(among=[manual_id])
    for factor in ("email", "phone"):
        verification = lease.verification(factor)
        await verification.start()
        await verification.submit()
    kyc = lease.verification("kyc_id")
    await kyc.start(KycEvidence(document_number=SecretStr("SECRET-DOCUMENT")))
    await kyc.submit()

    drift_id = await exchange("drift@example.test")
    await space.identities.override(
        drift_id,
        Override(
            "standing",
            "",
            Standing.GOOD,
            reason="false positive",
            actor="admin",
            pin_for=timedelta(hours=6),
        ),
    )
    await space._observe(
        drift_id,
        "standing",
        Observation(Standing.BANNED, now, raw_status="BAN"),
    )

    quarantine_id = await exchange("quarantine@example.test")
    await space._command(
        quarantine_id,
        "availability",
        Command(Availability.QUARANTINED, actor="worker"),
    )

    foreign = await register(
        other,
        service="fake_tg",
        external_ref="foreign",
        credentials={"phone": "foreign"},
        lifecycle="ready",
    )
    await other.verifications.requirements.add(foreign.identity_id, "must-not-leak")

    first = await space.admin.human_queue(limit=2)
    second = await space.admin.human_queue(limit=20, after=first.next_cursor)
    items = (*first.items, *second.items)

    assert first.next_cursor is not None
    assert {item.kind for item in items} == {
        "challenge",
        "requirement",
        "manual_review",
        "drift",
        "quarantine",
    }
    assert [item.urgency for item in items] == ["critical", "high", "high", "high", "high"]
    challenge = next(item for item in items if item.kind == "challenge")
    assert challenge.expired and challenge.details == {"kind": "otp", "hint": "***42"}
    assert all(item.action_unavailable_reason for item in items)
    assert {item.kind: item.action_available for item in items} == {
        "challenge": False,
        "requirement": True,
        "manual_review": True,
        "drift": True,
        "quarantine": True,
    }
    assert "never-return-this-secret" not in repr(items)
    assert "SECRET-DOCUMENT" not in repr(items)
    assert "secret-password" not in repr(items)
    assert foreign.identity_id not in {item.identity_id for item in items}
    assert first.counts == {
        "challenge": 1,
        "drift": 1,
        "manual_review": 1,
        "quarantine": 1,
        "requirement": 1,
    }
    assert [item.kind for item in (await space.admin.human_queue(kind="requirement")).items] == [
        "requirement"
    ]
    with pytest.raises(ValidationFailed):
        await space.admin.human_queue(after="not-valid-base64!")


async def test_proxy_reads_match_bindings_and_redact_userinfo(memory_hub: IdentityHub) -> None:
    service = replace(
        FAKE_TG,
        name="proxy_read_tg",
        proxy_policy=ProxyPolicy(group="res", max_identities=2),
    )
    memory_hub.register_service(service)
    await memory_hub.start()
    space = memory_hub.space("proxy-read", "Proxy read", create=True)
    proxy_id, _ = await add_proxy(
        space,
        "socks5://private-user:private-password@1.2.3.4:1080",
        pool="res",
        max_identities=2,
    )
    async with space._tx() as transaction:
        assert await transaction.update_proxy(proxy_id, {"status": "alive"})
    await add_proxy(space, "http://5.6.7.8:3128", pool="backup", max_identities=4)
    identities = []
    for index in range(2):
        registered = await register(
            space,
            service=service.name,
            external_ref=f"proxy-{index}",
            credentials={"phone": f"+1000{index}"},
            session={"auth_key": f"key-{index}", "dc_id": 2},
            lifecycle="ready",
        )
        identities.append(registered.identity_id)
    first = await space.pools.define("workers", service=service.name).acquire(among=[identities[0]])
    second = await space.pools.define("workers", service=service.name).acquire(among=[identities[1]])
    await second.release()

    page_one = await space.admin.proxy_overview(limit=1)
    page_two = await space.admin.proxy_overview(limit=10, before=page_one.next_before)
    all_items = (*page_one.items, *page_two.items)
    selected = next(item for item in all_items if item.id == proxy_id)
    full = await space.admin.proxy_overview(filters=ProxyFilters(capacity="full"))
    detail_one = await space.admin.proxy_detail(proxy_id, history_limit=1)
    detail_two = await space.admin.proxy_detail(
        proxy_id,
        history_limit=10,
        history_before=detail_one.history.next_before,
    )

    assert page_one.next_before is not None
    assert selected.masked_url == "socks5://1.2.3.4:1080"
    assert selected.bound_count == 2 and selected.available_capacity == 0
    assert selected.in_lease_count == 1
    assert [item.id for item in full.items] == [proxy_id]
    assert len(detail_one.bound_identities) == 2
    assert detail_one.history.next_before is not None
    assert len(detail_one.history.items) + len(detail_two.history.items) == 2
    assert "private-user" not in repr((page_one, page_two, detail_one, detail_two))
    assert "private-password" not in repr((page_one, page_two, detail_one, detail_two))
    pool = next(item for item in page_one.pools if item.pool == "res")
    assert (pool.bound_count, pool.max_identities, pool.in_lease_count) == (2, 2, 1)
    assert not selected.actions_available

    other = memory_hub.space("proxy-other", "Proxy other", create=True)
    foreign_id, _ = await add_proxy(other, "http://9.9.9.9:80", pool="res")
    with pytest.raises(NotFound, match="proxy"):
        await space.admin.proxy_detail(foreign_id)
    await first.release()


@pytest.fixture
async def shared_memory_hub() -> IdentityHub:
    # системные часы: порядок выдачи shared-аккаунтов зависит от времени захвата
    hub = IdentityHub(
        secrets=StaticSecretsProvider(TEST_KEYS, "k1"),
        services=[FAKE_TG, SHARED_TG],
        store=MemoryStore(),
        strict_events=True,
    )
    await hub.start()
    return hub


async def test_shared_parallel_leases_keep_writing(shared_memory_hub: IdentityHub) -> None:
    await check_parallel_shared_leases_keep_writing(shared_memory_hub)


async def test_shared_rotates_least_recently_leased(shared_memory_hub: IdentityHub) -> None:
    await check_shared_rotates_least_recently_leased(shared_memory_hub)


async def test_shared_rejects_idempotency_key(shared_memory_hub: IdentityHub) -> None:
    await check_shared_rejects_idempotency_key(shared_memory_hub)
