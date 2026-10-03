"""F3.2: параллельная доставка вебхуков (R1-05)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from identity_hub import IdentityHub, StaticSecretsProvider, ValidationFailed
from identity_hub.dispatch.webhooks import WebhookDispatcher, WebhookPolicy
from identity_hub.events import EventDraft
from identity_hub.testing import MemoryStore
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.unit

SLOW = "https://slow.example.test/hook"
FAST = "https://fast.example.test/hook"


async def _hub(policy: WebhookPolicy) -> IdentityHub:
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"\x33" * 32}, "k1"),
        services=[FAKE_TG],
        store=MemoryStore(),
        webhooks=policy,
    )
    await hub.start()
    return hub


async def _emit(hub: IdentityHub, count: int) -> Any:
    space = hub.space("hooks", "Hooks", create=True)
    for n in range(count):
        async with space._tx(own=True) as tx:
            await space._emit(tx, EventDraft("identity.updated", actor="test", data={"n": n}))
    return space


@respx.mock
async def test_slow_endpoint_does_not_delay_other_endpoints_or_pumps() -> None:
    """R1-05: недоступный получатель стоит пачке одного таймаута и не задерживает остальных."""
    hub = await _hub(WebhookPolicy(timeout=0.5, claim_for=timedelta(seconds=3)))
    space = hub.space("hooks", "Hooks", create=True)
    slow = await space.webhooks.create(url=SLOW, secret=SecretStr("s"), event_types=["identity.updated"])
    fast = await space.webhooks.create(url=FAST, secret=SecretStr("s"), event_types=["identity.updated"])
    loop = asyncio.get_running_loop()
    marks: dict[str, list[float]] = {"fast": [], "pump": []}

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.4)
        raise httpx.ConnectTimeout("no answer")

    async def answer(request: httpx.Request) -> httpx.Response:
        marks["fast"].append(loop.time())
        return httpx.Response(204)

    async def other_pump() -> int:
        marks["pump"].append(loop.time())
        return 0

    slow_route = respx.post(SLOW).mock(side_effect=hang)
    respx.post(FAST).mock(side_effect=answer)
    hub.dispatcher.add_pump(other_pump)
    await _emit(hub, 3)

    started = loop.time()
    await hub.dispatcher.run_once()
    elapsed = loop.time() - started

    assert len(marks["fast"]) == 3 and max(marks["fast"]) - started < 0.3
    assert marks["pump"] and marks["pump"][0] - started < 0.3
    assert slow_route.call_count == 1  # после сетевой ошибки остальные доставки endpoint'а отложены
    assert elapsed < 1.0  # один таймаут, а не три подряд
    assert [row.status for row in await space.webhooks.deliveries(fast.id)] == ["succeeded"] * 3
    slow_rows = sorted(await space.webhooks.deliveries(slow.id), key=lambda row: row.id)
    assert [(row.status, row.attempt) for row in slow_rows] == [("failed", 1), ("pending", 0), ("pending", 0)]
    assert all(row.next_attempt_at > hub.clock.now() for row in slow_rows)


@respx.mock
async def test_delivery_is_not_repeated_when_claim_expires() -> None:
    """R1-05: пачка, не уложившаяся в срок захвата, не доставляется вторым экземпляром повторно."""
    policy = WebhookPolicy(timeout=0.3, claim_for=timedelta(seconds=1))
    hub = await _hub(policy)
    space = hub.space("hooks", "Hooks", create=True)
    endpoint = await space.webhooks.create(url=SLOW, secret=SecretStr("s"), event_types=["identity.updated"])
    posted: list[str] = []

    async def answer(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.25)
        posted.append(json.loads(request.content)["event_id"])
        return httpx.Response(204)

    respx.post(SLOW).mock(side_effect=answer)
    await _emit(hub, 6)
    await hub._store.process_events("webhooks", hub._webhook_dispatcher.handle_events, limit=100)
    first, second = hub._webhook_dispatcher, WebhookDispatcher(hub, policy)  # два экземпляра диспетчера

    running = asyncio.create_task(first.deliver_due())
    await asyncio.sleep(1.05)  # срок захвата первой пачки истёк
    await asyncio.gather(running, second.deliver_due())
    for _ in range(10):
        if await first.deliver_due() == 0:
            break

    assert len(posted) == 6 and len(set(posted)) == 6
    rows = await space.webhooks.deliveries(endpoint.id)
    assert [(row.status, row.attempt) for row in rows] == [("succeeded", 1)] * 6


@respx.mock
async def test_one_endpoint_gets_one_request_at_a_time_and_endpoints_run_in_parallel() -> None:
    hub = await _hub(WebhookPolicy(timeout=1.0, claim_for=timedelta(seconds=10), concurrency=2))
    space = hub.space("hooks", "Hooks", create=True)
    urls = [f"https://h{n}.example.test/hook" for n in range(4)]
    for url in urls:
        await space.webhooks.create(url=url, secret=SecretStr("s"), event_types=["identity.updated"])
    active: dict[str, int] = {}
    peak_per_host: dict[str, int] = {}
    peak_total = 0

    async def answer(request: httpx.Request) -> httpx.Response:
        nonlocal peak_total
        host = request.url.host
        active[host] = active.get(host, 0) + 1
        peak_per_host[host] = max(peak_per_host.get(host, 0), active[host])
        peak_total = max(peak_total, sum(active.values()))
        await asyncio.sleep(0.05)
        active[host] -= 1
        return httpx.Response(204)

    respx.post(url__regex=r"https://h\d\.example\.test/hook").mock(side_effect=answer)
    await _emit(hub, 3)

    await hub.dispatcher.run_once()

    assert set(peak_per_host.values()) == {1}  # на endpoint — одна доставка за раз
    assert peak_total == 2  # WebhookPolicy.concurrency


def test_policy_rejects_timeout_not_shorter_than_claim() -> None:
    with pytest.raises(ValidationFailed):
        WebhookPolicy(timeout=30, claim_for=timedelta(seconds=30))
    with pytest.raises(ValidationFailed):
        WebhookPolicy(concurrency=0)
    policy = WebhookPolicy()
    assert policy.concurrency == 8
    assert 0 < policy.start_window < policy.claim_for.total_seconds() - policy.timeout
