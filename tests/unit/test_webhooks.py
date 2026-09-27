"""P8.3: чистое ядро исходящих вебхуков (FR-14.1, FR-14.2, S-9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from identity_hub.dispatch.webhooks import (
    WebhookPolicy,
    make_signature,
    validate_webhook_url,
    verify_signature,
)
from identity_hub.errors import ValidationFailed

pytestmark = pytest.mark.unit


def test_signature_supports_rotation_and_replay_window() -> None:
    body = b'{"event_id":"evt-1"}'
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    old = SecretStr("old-secret")
    new = SecretStr("new-secret")
    header = make_signature(body, [new, old], timestamp=int(now.timestamp()))

    assert header.startswith(f"t={int(now.timestamp())},v1=")
    assert header.count("v1=") == 2
    assert verify_signature(body, header, new, now=now)
    assert verify_signature(body, header, old, now=now)
    assert not verify_signature(body + b" ", header, new, now=now)
    assert not verify_signature(body, header, new, now=now + timedelta(seconds=301))
    assert not verify_signature(body, "broken", new, now=now)


def test_retry_delay_is_exponential_and_capped_at_24_hours() -> None:
    policy = WebhookPolicy(retry_base=timedelta(minutes=1), retry_jitter=0)
    assert policy.retry_after(1) == timedelta(minutes=1)
    assert policy.retry_after(5) == timedelta(minutes=16)
    assert policy.retry_after(99) == timedelta(hours=24)


def test_default_request_timeout_is_ten_seconds() -> None:
    assert WebhookPolicy().timeout == 10.0


@pytest.mark.parametrize(
    "url,dev_hosts,ok",
    [
        ("https://hooks.example.test/identity", (), True),
        ("http://localhost:9000/hook", ("localhost",), True),
        ("http://127.0.0.1/hook", ("localhost",), False),
        ("http://hooks.example.test/hook", (), False),
        ("ftp://hooks.example.test/hook", ("hooks.example.test",), False),
        ("https:///missing-host", (), False),
    ],
)
def test_webhook_url_policy(url: str, dev_hosts: tuple[str, ...], ok: bool) -> None:
    if ok:
        assert validate_webhook_url(url, dev_hosts=dev_hosts) == url
    else:
        with pytest.raises(ValidationFailed):
            validate_webhook_url(url, dev_hosts=dev_hosts)
