# Identity Hub

Identity Hub — асинхронная Python-библиотека для управления парком внешних
аккаунтов: их состояниями, эксклюзивной арендой, квотами, прокси, зашифрованными
сессиями и событиями. Основной читатель этого руководства — разработчик Python
3.12+, который встраивает библиотеку в воркеры или веб-приложение.

Библиотека подходит, когда несколько процессов должны безопасно делить аккаунты
Telegram, бирж или других платформ. Она не является клиентом этих платформ:
сетевой вызов выполняет код приложения, а Identity Hub решает, какой аккаунт
можно взять, и фиксирует результат операции.

## Установка через uv

Для PostgreSQL и встроенных миграций Alembic:

```bash
uv add "identity-hub[postgres,alembic]"
```

Дополнительные зависимости подключаются отдельно: `telegram` для транспорта
уведомлений через Telegram Bot API (`httpx`, без SDK), `webhooks` для HTTP-вебхуков, `structlog` для
структурированных логов и `testing` для тестового `MemoryStore` и pytest-плагина.

Для разработки самого репозитория:

```bash
uv sync --all-extras
docker compose up -d postgres
uv run pytest
```

## Быстрый старт

1. Создайте 32-байтовый мастер-ключ. Не коммитьте его и не выводите в логи:

   ```bash
   uv run python -c "from identity_hub.crypto import generate_key; print(generate_key())"
   ```

2. Сохраните результат в `IDENTITY_HUB_KEK`. Первый ключ в строке активен;
   прежние ключи оставляют после запятой на время ротации.

3. Задайте URL PostgreSQL и примените миграции:

   ```bash
   export IDENTITY_HUB_DATABASE_URL='postgresql+asyncpg://app:password@localhost/app'
   export IDENTITY_HUB_KEK='k1:<base64-ключ>'
   uv run identity-hub migrate
   ```

4. Объявите сервис в коде и создайте один `IdentityHub` на процесс:

   ```python
   from datetime import timedelta

   from pydantic import BaseModel, SecretStr
   from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

   from identity_hub import EnvSecretsProvider, IdentityHub, OperationRule, Quota, Service


   class TelegramCredentials(BaseModel):
       phone: str


   class TelegramSession(BaseModel):
       auth_key: SecretStr
       dc_id: int = 2


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

   engine = create_async_engine(
       "postgresql+asyncpg://app:password@localhost/app",
       pool_pre_ping=True,
       hide_parameters=True,  # параметры запросов не попадают в тексты ошибок и логи
   )
   sessions = async_sessionmaker(engine, expire_on_commit=False)
   hub = IdentityHub(
       sessionmaker=sessions,
       secrets=EnvSecretsProvider(),
       services=[TELEGRAM],
   )
   ```

5. На старте приложения синхронизируйте реестр сервисов, затем работайте через
   пространство и пул:

   ```python
   from identity_hub import AuthState, IdentityCreate, Lifecycle

   await hub.start()
   space = await hub.spaces.ensure("production", "Production")

   await space.identities.create(
       IdentityCreate(
           service=TELEGRAM,
           external_ref="+12025550123",
           credentials={"phone": "+12025550123"},
           session={"auth_key": "secret-session"},
           tags=["parser"],
           lifecycle=Lifecycle.READY,
           auth=AuthState.AUTHENTICATED,
       )
   , actor="operator")

   parsers = space.pools.define("parsers", service=TELEGRAM, tags=["parser"])
   async with parsers.lease(for_operation="read_channel") as lease:
       session = await lease.session()
       async with lease.operation("read_channel", key="job:42"):
           # Здесь приложение вызывает Telegram-клиент с session и lease.proxy.
           messages = await telegram_client.read_history(session)
   ```

`for_operation` отбрасывает аккаунты, которые не удовлетворяют состояниям или
верификации. Квоты при выборе не учитываются: контекст `operation` повторно
проверяет состояние и атомарно списывает квоту до внешнего вызова, а при
исчерпании бросает `QuotaExhausted`. Параметр `key` делает списание конкретной
операции идемпотентным.

Полный самодостаточный сценарий находится в
[`examples/telegram_service.py`](examples/telegram_service.py). Он использует
память вместо PostgreSQL и имитирует платформенный клиент, поэтому запускается
без внешних сервисов:

```bash
uv run python examples/telegram_service.py
```

## Руководство

Как добавлять и регистрировать аккаунты, какие бывают состояния и кто ими управляет,
как банить, верифицировать и синхронизировать статусы с платформой, какие бывают
аренды и лимиты — в [руководстве](docs/guide/README.md) с диаграммами архитектуры и
машин состояний. Сценарий из руководства целиком исполняется в
[`examples/account_lifecycle.py`](examples/account_lifecycle.py).

## Эксплуатация и тесты

- [CLI и переменные окружения](docs/cli.md) — миграции, диспетчер,
  обслуживание, диагностика и ротация ключей.
- [Интеграция](docs/integrations.md) — Alembic, отдельная схема PostgreSQL,
  PgBouncer, Dishka и жизненный цикл процессов.
- [Нагрузочные характеристики](docs/perf.md) — воспроизводимый стенд и
  известные ограничения производительности.
- [Трассировка требований](docs/traceability.md) — каждый FR/G/T/S и его
  исполняемое тестовое доказательство.
- [CHANGELOG](CHANGELOG.md) — пользовательские изменения по версиям.

Для быстрых тестов без БД установите extra `testing`. Pytest-плагин предоставляет
фикстуры с префиксом `identity_hub_`, а пользовательский пример можно выполнить
командой:

```bash
uv run pytest examples/test_memory_hub.py
```

`MemoryStore` предназначен для тестов и однопроцессных примеров. Конкурентные
воркеры, долговечные аренды и эксплуатационный журнал требуют PostgreSQL.

## Границы безопасности

- Сессии, credentials, секреты прокси и токены доставок шифруются отдельными
  DEK; мастер-ключи предоставляет приложение.
- Расшифрованные credentials и сессии воркер получает через объект действующей аренды: после
  освобождения, потери или отзыва он бросает `LeaseLost`. Вне аренды секрет раскрывают
  аудируемые команды `space.secrets.reveal` и `space.proxies.url` (автор и причина обязательны).
- Потеря heartbeat отменяет владеющую задачу и вызывает `on_lost`; сетевой
  клиент должен быть отключён этим callback.
- Диспетчер доставляет события как минимум один раз. Обработчики обязаны быть
  идемпотентными.
- `SpaceHandle.using(db_session)` нужен, когда изменение Identity Hub должно
  участвовать во внешней транзакции приложения; обычные методы используют свои
  короткие транзакции.

Полная спецификация поведения —
[`identity-hub-requirements.md`](identity-hub-requirements.md), история
реализации — [`docs/plan/PLAN.md`](docs/plan/PLAN.md).

## In English

Identity Hub is an async Python library (3.12+, PostgreSQL 16) for managing a fleet of external
accounts: their states, exclusive leases with heartbeat and fencing, rate limits, proxies,
encrypted sessions and an event stream. It does not talk to the platforms itself — your code
makes the network calls, the library decides which account may be used and records the outcome.

- **Install:** `uv add "identity-hub[postgres,alembic]"`, then `identity-hub migrate` against
  your database.
- **Status:** alpha (`1.0.0aN`). The public API may still change between alpha releases;
  every breaking change is marked in `CHANGELOG.md`.
- **Language:** documentation, error messages and logs are in Russian. Identifiers, error codes,
  event types and field names are English and stable for programmatic use.

## Лицензия

[MIT](LICENSE). Код поставляется «как есть», без каких-либо гарантий.
