"""Telegram-транспорт: прямой вызов Bot API `sendMessage` через httpx (опциональный extra `telegram`, D-139).

Токен бота — часть URL (`/bot<token>/sendMessage`), поэтому исключения httpx, содержащие URL,
никогда не выпускаются наружу: транспорт поднимает собственные исключения без токена и без цепочки.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from identity_hub.dispatch.notifications import TelegramRateLimited, TelegramRejected, TelegramUnavailable

if TYPE_CHECKING:
    import httpx

__all__ = ["HttpTelegramTransport"]

API_URL = "https://api.telegram.org"
# Ошибки запроса, которые не исправятся повтором: неверный токен, чат не найден, бот заблокирован.
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404})


class HttpTelegramTransport:
    """Реализация `TelegramTransport`.

    Без внешнего `client` транспорт лениво создаёт один `httpx.AsyncClient` и переиспользует его;
    закрыть — `await transport.aclose()`. Внешний клиент транспорт не закрывает.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        api_url: str = API_URL,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._api_url = api_url.rstrip("/")

    async def send_message(
        self,
        bot_token: SecretStr,
        chat_id: int | str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        client = self._get_client()
        url = f"{self._api_url}/bot{bot_token.get_secret_value()}/sendMessage"
        try:
            response = await client.post(url, json=payload, follow_redirects=False)
        except Exception as exc:
            raise TelegramUnavailable(type(exc).__name__) from None
        if response.status_code == 200:
            return
        body = _json(response)
        if response.status_code == 429:
            parameters = body.get("parameters")
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            raise TelegramRateLimited(retry_after if isinstance(retry_after, int | float) else 1)
        description = body.get("description")
        detail = f"HTTP {response.status_code}" + (f": {description}" if isinstance(description, str) else "")
        if response.status_code in _PERMANENT_STATUSES:
            raise TelegramRejected(response.status_code, detail)
        raise TelegramUnavailable(detail)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - окружение без extra telegram
                raise RuntimeError("для Telegram установите identity-hub[telegram]") from exc
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        return self._client


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
