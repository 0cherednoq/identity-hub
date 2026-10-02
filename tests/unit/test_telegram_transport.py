"""FR-15.1: Telegram-транспорт на httpx (прямой вызов Bot API, D-139)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from identity_hub.dispatch.notifications import TelegramRateLimited, TelegramRejected
from identity_hub.telegram_notify import HttpTelegramTransport

pytestmark = pytest.mark.unit

TOKEN = "123456:telegram-secret"
URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@respx.mock
async def test_sends_message_with_thread() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))

    await HttpTelegramTransport().send_message(SecretStr(TOKEN), -100123, "alert", thread_id=7)

    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {
        "chat_id": -100123,
        "text": "alert",
        "message_thread_id": 7,
    }


@respx.mock
async def test_thread_is_omitted_when_absent() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))

    await HttpTelegramTransport().send_message(SecretStr(TOKEN), "@ops", "alert")

    assert json.loads(route.calls.last.request.content) == {"chat_id": "@ops", "text": "alert"}


@respx.mock
async def test_injected_client_is_reused_and_not_closed() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    async with httpx.AsyncClient() as client:
        transport = HttpTelegramTransport(client=client)
        await transport.send_message(SecretStr(TOKEN), 1, "a")
        await transport.send_message(SecretStr(TOKEN), 1, "b")
        await transport.aclose()
        assert not client.is_closed


@respx.mock
async def test_429_is_normalized_to_retry_after() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 5",
                "parameters": {"retry_after": 5},
            },
        )
    )

    with pytest.raises(TelegramRateLimited) as caught:
        await HttpTelegramTransport().send_message(SecretStr(TOKEN), 1, "alert")

    assert caught.value.retry_after == 5


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@respx.mock
async def test_client_errors_are_permanent_rejections(status: int) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(
            status,
            json={"ok": False, "error_code": status, "description": "Forbidden: bot was blocked by the user"},
        )
    )

    with pytest.raises(TelegramRejected) as caught:
        await HttpTelegramTransport().send_message(SecretStr(TOKEN), 1, "alert")

    assert caught.value.status == status
    assert "bot was blocked" in str(caught.value)
    assert TOKEN not in str(caught.value)


@respx.mock
async def test_server_error_is_transient_and_hides_token() -> None:
    respx.post(URL).mock(return_value=httpx.Response(502, text="Bad Gateway"))

    with pytest.raises(Exception) as caught:
        await HttpTelegramTransport().send_message(SecretStr(TOKEN), 1, "alert")

    assert not isinstance(caught.value, TelegramRejected | TelegramRateLimited)
    assert str(caught.value) == "HTTP 502"


@respx.mock
async def test_network_error_hides_token() -> None:
    respx.post(URL).mock(side_effect=httpx.ConnectError(f"cannot connect to {URL}"))

    with pytest.raises(Exception) as caught:
        await HttpTelegramTransport().send_message(SecretStr(TOKEN), 1, "alert")

    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None  # исключение httpx с URL не остаётся в цепочке
