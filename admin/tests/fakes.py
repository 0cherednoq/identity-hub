"""In-memory transports that never perform network I/O."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import SecretStr


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    chat_id: int | str
    text: str
    thread_id: int | None


@dataclass(slots=True)
class RecordingTelegramTransport:
    messages: list[TelegramMessage] = field(default_factory=list)

    async def send_message(
        self,
        bot_token: SecretStr,
        chat_id: int | str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        assert bot_token.get_secret_value(), "test token must be present"
        self.messages.append(TelegramMessage(chat_id, text, thread_id))


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    url: str
    body: bytes
    headers: dict[str, str]


@dataclass(slots=True)
class RecordingWebhookTransport:
    requests: list[WebhookRequest] = field(default_factory=list)

    async def post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.requests.append(WebhookRequest(url, body, dict(headers)))
        return 204
