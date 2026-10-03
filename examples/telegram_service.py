"""Самодостаточный пример Telegram-подобного сервиса из ТЗ §11.

Запуск: ``uv run python examples/telegram_service.py``.
Внешняя сеть и PostgreSQL не требуются: MemoryStore и FakeTelegramClient
позволяют использовать этот файл как интеграционный тест публичного API.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from pydantic import BaseModel, SecretStr

from identity_hub import (
    AuthState,
    Event,
    IdentityCreate,
    IdentityHub,
    Lifecycle,
    OperationRule,
    Quota,
    Service,
    StaticSecretsProvider,
)
from identity_hub.testing import MemoryStore


class TelegramCredentials(BaseModel):
    phone: str


class TelegramSession(BaseModel):
    auth_key: SecretStr
    dc_id: int = 2
    cursor: int = 0


TELEGRAM = Service(
    name="telegram",
    title="Telegram",
    credentials=TelegramCredentials,
    session=TelegramSession,
    operations={
        "read_channel": OperationRule(
            quotas=(Quota(100, timedelta(hours=1)),),
        ),
    },
)


class FakeTelegramClient:
    """Минимальная граница с платформенным SDK, заменённая тестовым дублем."""

    def __init__(self) -> None:
        self.disconnected = False

    async def read_history(self, session: TelegramSession) -> list[str]:
        if self.disconnected:
            raise RuntimeError("клиент уже отключён")
        return [f"message-after-{session.cursor}"]

    async def disconnect(self) -> None:
        self.disconnected = True


@dataclass(frozen=True)
class ExampleResult:
    messages: tuple[str, ...]
    event_types: tuple[str, ...]
    saved_cursor: int


async def run_example() -> ExampleResult:
    """Зарегистрировать аккаунт, выполнить операцию и доставить события."""
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"example": bytes(range(32))}, active="example"),
        services=[TELEGRAM],
    )
    received: list[Event] = []

    async def collect(event: Event) -> None:
        received.append(event)

    hub.events.subscribe(
        "telegram-example",
        collect,
        types={"identity.registered", "identity.updated"},
    )
    await hub.start()

    space = hub.space("tennet", "Tennet", create=True)
    registered = await space.identities.create(
        IdentityCreate(
            service=TELEGRAM,
            external_ref="+12025550123",
            credentials=TelegramCredentials(phone="+12025550123"),
            session=TelegramSession(auth_key=SecretStr("test-session")),
            tags=["parser"],
            lifecycle=Lifecycle.READY,
            auth_state=AuthState.AUTHENTICATED,
        ),
        actor="telegram-example",
    )

    client = FakeTelegramClient()
    pool = space.pools.define(
        "parsers",
        service=TELEGRAM,
        tags=["parser"],
        holder="telegram-example",
    )
    async with pool.lease(for_operation="read_channel", on_lost=client.disconnect) as lease:
        session = await lease.session()
        if not isinstance(session, TelegramSession):
            raise RuntimeError("у готового аккаунта должна быть Telegram-сессия")
        async with lease.operation("read_channel", key="example:history"):
            messages = await client.read_history(session)
        saved = session.model_copy(update={"cursor": session.cursor + len(messages)})
        await lease.save_session(saved)

    # Пользовательские события доставляются после коммита. События lease.*,
    # operation.* и session.saved остаются во внутреннем журнале.
    await space.identities.set_note(registered.id, "history read", actor="telegram-example")
    await hub.dispatcher.drain()
    return ExampleResult(
        messages=tuple(messages),
        event_types=tuple(event.type for event in received),
        saved_cursor=saved.cursor,
    )


def main() -> None:
    result = asyncio.run(run_example())
    print(f"messages={len(result.messages)} saved_cursor={result.saved_cursor}")
    print("events=" + ",".join(result.event_types))


if __name__ == "__main__":
    main()
