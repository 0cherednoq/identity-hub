"""F1.5: исходящие запросы — SSRF вебхуков, `trust_env`, секреты в логах httpx.

Находки R1-04, R2-S02, R2-S15.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpcore
import httpx
import pytest
import respx
from pydantic import SecretStr

from identity_hub.crypto import StaticSecretsProvider
from identity_hub.dispatch.notifications import TelegramUnavailable
from identity_hub.dispatch.outbound import (
    ForbiddenTarget,
    PinnedBackend,
    create_client,
    host_address,
    is_public_address,
    mask_url,
    masked_in_logs,
    url_log_masks,
)
from identity_hub.dispatch.webhooks import WebhookPolicy, validate_webhook_url
from identity_hub.errors import ValidationFailed
from identity_hub.hub import IdentityHub
from identity_hub.telegram_notify import HttpTelegramTransport
from identity_hub.testing import MemoryStore
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

pytestmark = pytest.mark.unit

PUBLIC = "93.184.216.34"


# ---------------------------------------------------------------- проверка URL


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.5/hook",
        "https://169.254.169.254/latest/",
        "https://[::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://[::ffff:10.0.0.1]/x",
        "https://2130706433/x",  # десятичная запись 127.0.0.1
        "https://0x7f000001/x",  # шестнадцатеричная
        "https://127.1/x",  # сокращённая
        "https://0177.0.0.1/x",  # восьмеричная
        "https://0x7f.0.0.1/x",
        "https://localhost./x",
        "https://foo.internal/x",
        "https://host.docker.internal/x",
        "https://metadata/computeMetadata/v1/",  # имя без точки ищется в search-домене
        "https://redis/x",
        "https://printer.local/x",
    ],
)
def test_validate_webhook_url_rejects_local_targets(url: str) -> None:
    with pytest.raises(ValidationFailed):
        validate_webhook_url(url)


@pytest.mark.parametrize("url", ["https://hooks.example.com/x", "https://8.8.8.8/x", "https://134744072/x"])
def test_validate_webhook_url_accepts_public_targets(url: str) -> None:
    assert validate_webhook_url(url) == url


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("10.1.257", "10.1.1.1"),
        ("[::1]", "::1"),
        ("example.com", None),
        ("1.2.3.4.5", None),
        ("999.1.1.1", None),
    ],
)
def test_host_address_understands_resolver_forms(host: str, expected: str | None) -> None:
    address = host_address(host)
    assert (str(address) if address is not None else None) == expected


def test_ipv4_mapped_addresses_are_checked_by_embedded_ipv4() -> None:
    import ipaddress

    assert not is_public_address(ipaddress.ip_address("::ffff:10.0.0.1"))
    assert is_public_address(ipaddress.ip_address("::ffff:8.8.8.8"))
    assert not is_public_address(ipaddress.ip_address("224.0.0.1"))


# ---------------------------------------------------------------- закрепление адреса


class _Backend:
    """Внутренний сетевой бэкенд: записывает адрес соединения и отдаёт готовый HTTP-ответ."""

    def __init__(self, *, fail: Sequence[str] = ()) -> None:
        self.connected: list[tuple[str, int]] = []
        self._fail = set(fail)

    async def connect_tcp(self, host: str, port: int, **_kwargs: Any) -> Any:
        self.connected.append((host, port))
        if host in self._fail:
            raise httpcore.ConnectError("refused")
        return httpcore.AsyncMockStream([b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"])

    async def sleep(self, seconds: float) -> None:  # pragma: no cover
        return None


def _resolver(mapping: dict[str, list[str]]) -> Any:
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return mapping[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


async def test_connection_goes_to_validated_address() -> None:
    inner = _Backend()
    resolver = _resolver({"hooks.example.com": [PUBLIC]})
    backend = PinnedBackend(inner, resolver=resolver)

    await backend.connect_tcp("hooks.example.com", 443, timeout=1.0)

    assert inner.connected == [(PUBLIC, 443)]  # имя второй раз не резолвится: соединение с проверенным IP
    assert resolver.calls == ["hooks.example.com"]


@pytest.mark.parametrize(
    "addresses",
    [["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], [PUBLIC, "10.0.0.5"], ["::1"], ["fe80::1%eth0"], []],
    ids=["rfc1918", "loopback", "metadata", "mixed", "ipv6-loopback", "link-local-scope", "empty"],
)
async def test_name_resolving_to_private_address_is_refused(addresses: list[str]) -> None:
    inner = _Backend()
    backend = PinnedBackend(inner, resolver=_resolver({"internal-db.corp.example": addresses}))
    with pytest.raises(ForbiddenTarget):
        await backend.connect_tcp("internal-db.corp.example", 443)
    assert inner.connected == []


@pytest.mark.parametrize("host", ["127.0.0.1", "2130706433", "0x7f000001", "127.1", "unknown.example"])
async def test_literal_and_unresolvable_hosts_are_refused(host: str) -> None:
    inner = _Backend()
    with pytest.raises(ForbiddenTarget):
        await PinnedBackend(inner, resolver=_resolver({})).connect_tcp(host, 443)
    assert inner.connected == []


async def test_next_address_is_tried_and_dev_hosts_skip_checks() -> None:
    inner = _Backend(fail=["93.184.216.35"])
    backend = PinnedBackend(
        inner, resolver=_resolver({"hooks.example.com": ["93.184.216.35", PUBLIC]}), allow_hosts=["LocalHost"]
    )
    await backend.connect_tcp("hooks.example.com", 443)
    await backend.connect_tcp("localhost", 9000)
    assert inner.connected == [("93.184.216.35", 443), (PUBLIC, 443), ("localhost", 9000)]
    with pytest.raises(ForbiddenTarget):
        await backend.connect_unix_socket("/var/run/docker.sock")


async def test_client_keeps_url_and_host_header_while_pinning() -> None:
    """Через настоящий httpx: URL и Host — исходное имя, TCP — проверенный адрес."""
    client = create_client(timeout=5.0, pin=True, resolver=_resolver({"hooks.example.com": [PUBLIC]}))
    inner = _Backend()
    pool = client._transport._pool  # type: ignore[attr-defined]
    assert isinstance(pool._network_backend, PinnedBackend)
    pool._network_backend._inner = inner
    async with client:
        response = await client.post("http://hooks.example.com/hook", content=b"{}")
    assert response.status_code == 204
    assert str(response.request.url) == "http://hooks.example.com/hook"
    assert response.request.headers["host"] == "hooks.example.com"
    assert inner.connected == [(PUBLIC, 80)]


async def test_client_refuses_private_target_before_connecting() -> None:
    client = create_client(timeout=5.0, pin=True, resolver=_resolver({"evil.example": ["10.0.0.5"]}))
    async with client:
        with pytest.raises(ForbiddenTarget):
            await client.post("https://evil.example/hook", content=b"{}")


def test_clients_ignore_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://attacker.example:3128")
    client = create_client(timeout=5.0, pin=True)
    assert client.trust_env is False
    assert client._mounts == {}
    telegram = HttpTelegramTransport()._get_client()
    assert telegram.trust_env is False and telegram._mounts == {}


# ---------------------------------------------------------------- секреты в логах


def test_mask_url_hides_path_and_query() -> None:
    assert (
        mask_url("https://hooks.example.com:8443/T0/B0/SECRET?sig=abc")
        == "https://hooks.example.com:8443/***"
    )
    assert mask_url("https://[2001:db8::1]/x") == "https://[2001:db8::1]/***"


@respx.mock
async def test_webhook_path_and_query_are_masked_in_httpx_log(caplog: pytest.LogCaptureFixture) -> None:
    url = "https://hooks.example.com/services/T0/B0/WEBHOOK-SECRET?sig=QUERY-SECRET"
    respx.post(url).mock(return_value=httpx.Response(204))
    caplog.set_level(logging.DEBUG)
    async with create_client(timeout=5.0) as client:
        with masked_in_logs(*url_log_masks(url)):
            await client.post(url, content=b"{}")
    assert "HTTP Request: POST https://hooks.example.com/***" in caplog.text
    assert "WEBHOOK-SECRET" not in caplog.text and "QUERY-SECRET" not in caplog.text


@respx.mock
async def test_bot_token_is_masked_in_httpx_log(caplog: pytest.LogCaptureFixture) -> None:
    """R2-S02: httpx пишет URL каждого запроса на INFO, а токен бота — часть URL."""
    token = "123456:AAE-LEAK-BOT-TOKEN"
    respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    caplog.set_level(logging.DEBUG)
    transport = HttpTelegramTransport()
    await transport.send_message(SecretStr(token), 1, "hi")
    await transport.aclose()
    assert "HTTP Request: POST https://api.telegram.org/bot***/sendMessage" in caplog.text
    assert token not in caplog.text and "AAE-LEAK" not in caplog.text


@respx.mock
async def test_telegram_errors_do_not_keep_httpx_exception() -> None:
    token = "123456:AAE-LEAK-BOT-TOKEN"
    respx.post(f"https://api.telegram.org/bot{token}/sendMessage").mock(
        side_effect=httpx.ConnectError("boom")
    )
    transport = HttpTelegramTransport()
    with pytest.raises(TelegramUnavailable) as exc:
        await transport.send_message(SecretStr(token), 1, "hi")
    await transport.aclose()
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert token not in str(exc.value)


# ---------------------------------------------------------------- вебхуки хаба целиком


@pytest.fixture
async def hub() -> IdentityHub:
    hub = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"\x33" * 32}, "k1"), services=[FAKE_TG], store=MemoryStore()
    )
    await hub.start()
    return hub


SECRET_URL = "https://hooks.example.test/services/T0/WEBHOOK-SECRET?sig=QUERY-SECRET"


@pytest.mark.parametrize(
    "host", ["internal-db.corp.example", "127.0.0.1.nip.io", "kubernetes.default.svc"], ids=str
)
async def test_delivery_to_name_resolving_inside_is_refused(hub: IdentityHub, host: str) -> None:
    """R1-04: имена из `repro/secrets/a7.py` проходят проверку URL, но соединения с ними нет."""
    space = hub.space("ssrf", "SSRF")
    endpoint = await space.webhooks.create(
        url=f"https://{host}/hook", secret=SecretStr("s"), event_types=["identity.*"]
    )
    hub._webhook_dispatcher.resolver = _resolver({host: ["10.0.0.5"]})
    await register(space, service="fake_tg", external_ref="+ssrf")

    await hub.dispatcher.drain()

    delivery = (await space.webhooks.deliveries(endpoint.id))[0]
    assert (delivery.status, delivery.response_code, delivery.error) == ("failed", None, "ForbiddenTarget")
    with pytest.raises(ValidationFailed) as exc:
        await space.webhooks.send_test(endpoint.id)  # не оракул внутренних сервисов
    assert exc.value.__cause__ is None and exc.value.__context__ is None


async def test_dispatcher_client_pins_address_and_takes_proxy_only_from_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:3128")
    plain = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"\x33" * 32}, "k1"),
        store=MemoryStore(),
        webhook_dev_hosts=["localhost"],
    )
    client = plain._webhook_dispatcher.client()
    backend = client._transport._pool._network_backend  # type: ignore[attr-defined]
    assert isinstance(backend, PinnedBackend) and backend._allow == {"localhost"}
    assert client.trust_env is False and client._mounts == {}
    await client.aclose()

    proxied = IdentityHub(
        secrets=StaticSecretsProvider({"k1": b"\x33" * 32}, "k1"),
        store=MemoryStore(),
        webhooks=WebhookPolicy(proxy="http://egress.example:3128"),
    )
    client = proxied._webhook_dispatcher.client()
    pool = client._transport._pool  # type: ignore[attr-defined]
    assert isinstance(pool, httpcore.AsyncHTTPProxy)
    assert bytes(pool._proxy_url.host) == b"egress.example"
    await client.aclose()


@respx.mock
async def test_webhook_url_secret_is_not_exposed_by_space_api_or_logs(
    hub: IdentityHub, caplog: pytest.LogCaptureFixture
) -> None:
    """R2-S15: путь и query URL — секрет получателя; наружу отдаётся только `scheme://host/***`."""
    route = respx.post(SECRET_URL).mock(return_value=httpx.Response(204))
    caplog.set_level(logging.DEBUG)
    space = hub.space("masking", "Masking")
    endpoint = await space.webhooks.create(url=SECRET_URL, secret=SecretStr("s"), event_types=["identity.*"])
    await register(space, service="fake_tg", external_ref="+mask")

    await hub.dispatcher.drain()
    assert await space.webhooks.send_test(endpoint.id) == 204

    assert route.call_count == 2  # доставка идёт на полный URL
    assert endpoint.url == "https://hooks.example.test/***"
    assert "WEBHOOK-SECRET" not in repr(endpoint)
    assert "HTTP Request: POST https://hooks.example.test/***" in caplog.text
    assert "WEBHOOK-SECRET" not in caplog.text and "QUERY-SECRET" not in caplog.text


@respx.mock
async def test_failed_test_delivery_does_not_keep_url_in_exception_chain(hub: IdentityHub) -> None:
    respx.post(SECRET_URL).mock(side_effect=httpx.ConnectError("boom"))
    space = hub.space("masking", "Masking")
    endpoint = await space.webhooks.create(url=SECRET_URL, secret=SecretStr("s"), event_types=["*"])
    with pytest.raises(ValidationFailed, match="ConnectError") as exc:
        await space.webhooks.send_test(endpoint.id)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
