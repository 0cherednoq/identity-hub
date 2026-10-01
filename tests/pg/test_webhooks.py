"""P8.3: endpoints, доставка, ретраи и ручной повтор вебхуков (FR-14.1–14.5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from identity_hub.dispatch.webhooks import verify_signature
from identity_hub.enums import Severity
from identity_hub.hub import IdentityHub, SpaceHandle
from identity_hub.postgres.models import EventRecord, WebhookDeliveryRecord, WebhookEndpointRecord
from tests.fixtures.api import register

pytestmark = pytest.mark.pg


@respx.mock
async def test_filtered_delivery_is_signed_and_secret_is_encrypted(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    url = "https://hooks.example.test/identity"
    secret = SecretStr("signing-secret-that-must-not-leak")
    endpoint = await space.webhooks.create(
        url=url,
        secret=secret,
        event_types=["identity.*"],
        filter={"service": "fake_tg", "labels": {"farm": "3"}},
        min_severity=Severity.INFO,
        description="farm 3",
    )
    await space.webhooks.rotate_secret(endpoint.id, SecretStr("rotated-secret"), overlap=timedelta(hours=1))
    await register(space, service="fake_tg", external_ref="+1", labels={"farm": "3"})
    await register(space, service="fake_tg", external_ref="+2", labels={"farm": "4"})
    route = respx.post(url).mock(return_value=httpx.Response(204))

    await hub.dispatcher.drain()

    assert route.call_count == 1
    request = route.calls[0].request
    body = request.content
    signature = request.headers["X-Identity-Hub-Signature"]
    timestamp = int(signature.split(",", 1)[0].split("=", 1)[1])
    assert verify_signature(body, signature, SecretStr("rotated-secret"), now=timestamp)
    assert verify_signature(body, signature, secret, now=timestamp)
    assert request.headers["content-type"] == "application/json"
    assert b"signing-secret" not in body and b"rotated-secret" not in body

    async with sessionmaker() as session:
        stored = (
            await session.execute(
                select(WebhookEndpointRecord).where(WebhookEndpointRecord.id == endpoint.id)
            )
        ).scalar_one()
        delivery = (await session.execute(select(WebhookDeliveryRecord))).scalar_one()
    assert b"signing-secret" not in stored.ciphertext and b"rotated-secret" not in stored.ciphertext
    assert delivery.status == "succeeded"
    assert delivery.attempt == 1 and delivery.response_code == 204


@respx.mock
async def test_5xx_retries_disable_endpoint_and_manual_retry_recovers(
    hub: IdentityHub,
    space: SpaceHandle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hub.webhook_dispatcher.policy = replace(
        hub.webhook_dispatcher.policy,
        max_failures=2,
        retry_base=timedelta(0),
        retry_jitter=0,
    )
    url = "https://hooks.example.test/fail"
    endpoint = await space.webhooks.create(
        url=url,
        secret=SecretStr("secret"),
        event_types=["identity.registered"],
    )
    route = respx.post(url).mock(side_effect=[httpx.Response(503), httpx.Response(500), httpx.Response(200)])
    await register(space, service="fake_tg", external_ref="+3")

    await hub.dispatcher.drain()

    assert route.call_count == 2
    deliveries = await space.webhooks.deliveries(endpoint.id)
    assert len(deliveries) == 1
    failed = deliveries[0]
    assert failed.status == "dead" and failed.attempt == 2 and failed.response_code == 500
    async with sessionmaker() as session:
        stored = (
            await session.execute(
                select(WebhookEndpointRecord).where(WebhookEndpointRecord.id == endpoint.id)
            )
        ).scalar_one()
        disabled_event = (
            await session.execute(select(EventRecord).where(EventRecord.type == "webhook.endpoint.disabled"))
        ).scalar_one()
    assert not stored.enabled and stored.failure_streak == 2
    assert disabled_event.data == {"endpoint_id": str(endpoint.id), "failure_streak": 2}

    await space.webhooks.retry(failed.id)
    assert await hub.webhook_dispatcher.deliver_due() == 1
    assert route.call_count == 3
    retried = (await space.webhooks.deliveries(endpoint.id))[0]
    assert retried.status == "succeeded" and retried.attempt == 1


async def test_insecure_endpoint_is_rejected(space: SpaceHandle) -> None:
    with pytest.raises(Exception, match="https"):
        await space.webhooks.create(
            url="http://hooks.example.test/nope",
            secret=SecretStr("secret"),
            event_types=["*"],
        )


@respx.mock
async def test_redirect_is_recorded_without_following(
    hub: IdentityHub,
    space: SpaceHandle,
) -> None:
    source = "https://hooks.example.test/redirect"
    target = "https://other.example.test/secret-target"
    endpoint = await space.webhooks.create(
        url=source,
        secret=SecretStr("secret"),
        event_types=["identity.registered"],
    )
    source_route = respx.post(source).mock(return_value=httpx.Response(302, headers={"Location": target}))
    target_route = respx.post(target).mock(return_value=httpx.Response(204))
    await register(space, service="fake_tg", external_ref="+redirect")

    await hub.dispatcher.drain()

    assert source_route.call_count == 1
    assert target_route.call_count == 0
    delivery = (await space.webhooks.deliveries(endpoint.id))[0]
    assert delivery.status == "failed"
    assert delivery.response_code == 302
