"""Telegram-транспорт на aiogram (опциональный extra `telegram`)."""

from __future__ import annotations

from pydantic import SecretStr

from identity_hub.dispatch.notifications import TelegramRateLimited

__all__ = ["AiogramTelegramTransport"]


class AiogramTelegramTransport:
    async def send_message(
        self,
        bot_token: SecretStr,
        chat_id: int | str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        try:
            from aiogram import Bot
            from aiogram.exceptions import TelegramRetryAfter
        except ImportError as exc:  # pragma: no cover - окружение без extra telegram
            raise RuntimeError("для Telegram установите identity-hub[telegram]") from exc
        bot = Bot(bot_token.get_secret_value())
        try:
            await bot.send_message(chat_id, text, message_thread_id=thread_id)
        except TelegramRetryAfter as exc:
            raise TelegramRateLimited(exc.retry_after) from exc
        finally:
            await bot.session.close()
