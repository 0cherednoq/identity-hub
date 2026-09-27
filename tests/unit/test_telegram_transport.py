"""FR-15.1: опциональный aiogram-транспорт Telegram."""

from __future__ import annotations

from typing import ClassVar

import aiogram
import pytest
from pydantic import SecretStr

from identity_hub.dispatch.notifications import TelegramRateLimited
from identity_hub.telegram_notify import AiogramTelegramTransport

pytestmark = pytest.mark.unit


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeBot:
    instances: ClassVar[list[_FakeBot]] = []
    error: ClassVar[Exception | None] = None

    def __init__(self, token: str) -> None:
        self.token = token
        self.session = _FakeSession()
        self.calls: list[tuple[int | str, str, int | None]] = []
        self.instances.append(self)

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_thread_id: int | None,
    ) -> None:
        self.calls.append((chat_id, text, message_thread_id))
        if self.error is not None:
            raise self.error


async def test_aiogram_transport_sends_and_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeBot.instances.clear()
    _FakeBot.error = None
    monkeypatch.setattr(aiogram, "Bot", _FakeBot)

    await AiogramTelegramTransport().send_message(
        SecretStr("123:secret"),
        -100123,
        "alert",
        thread_id=7,
    )

    bot = _FakeBot.instances[-1]
    assert bot.token == "123:secret"
    assert bot.calls == [(-100123, "alert", 7)]
    assert bot.session.closed


async def test_aiogram_retry_after_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeRetryAfter(Exception):
        def __init__(self, retry_after: float) -> None:
            self.retry_after = retry_after

    import aiogram.exceptions

    _FakeBot.instances.clear()
    _FakeBot.error = FakeRetryAfter(4.5)
    monkeypatch.setattr(aiogram, "Bot", _FakeBot)
    monkeypatch.setattr(aiogram.exceptions, "TelegramRetryAfter", FakeRetryAfter)

    with pytest.raises(TelegramRateLimited) as caught:
        await AiogramTelegramTransport().send_message(SecretStr("123:secret"), "chat", "alert")

    assert caught.value.retry_after == 4.5
    assert _FakeBot.instances[-1].session.closed
