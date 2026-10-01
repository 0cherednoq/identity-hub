# Интеграция Identity Hub

Руководство адресовано разработчику, который подключает Identity Hub к уже
существующему асинхронному приложению на SQLAlchemy. Оно описывает владение
ресурсами и границы процессов, а не бизнес-модель аккаунтов.

## PostgreSQL и отдельная схема

Создайте один `AsyncEngine` приложения и один `async_sessionmaker`. Передавайте
тот же sessionmaker в `IdentityHub`; библиотека открывает короткие транзакции и
не удерживает транзакцию на всё время аренды.

```python
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from identity_hub.crypto import EnvSecretsProvider
from identity_hub.hub import IdentityHub

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
sessions = async_sessionmaker(engine, expire_on_commit=False)
hub = IdentityHub(
    sessionmaker=sessions,
    secrets=EnvSecretsProvider(),
    services=SERVICES,
    db_schema="identity_hub",
)
```

Значение `db_schema` должно совпадать при миграции и создании hub:

```bash
identity-hub migrate --db-schema identity_hub
```

Миграции создают схему и расширение `pg_trgm`, если требуется. Роль мигратора
должна иметь соответствующие права; runtime-роли достаточно прав на объекты
`ih_*` в выбранной схеме.

## Alembic приложения

Identity Hub владеет своей веткой миграций. Запускайте её отдельной командой или
программно, а объекты `ih_*` исключайте из autogenerate приложения:

```python
# alembic/env.py приложения
from identity_hub.alembic import exclude_identity_hub

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=exclude_identity_hub,
)
```

Если приложение уже имеет `include_object`, объедините предикаты:

```python
def include_object(obj, name, type_, reflected, compare_to):
    return exclude_identity_hub(obj, name, type_, reflected, compare_to) and app_filter(
        obj, name, type_, reflected, compare_to
    )
```

Программный запуск внутренней ветки:

```python
from identity_hub.postgres.migrate import migrate

await migrate(engine, db_schema="identity_hub")
```

Не добавляйте metadata моделей Identity Hub в metadata приложения: публичным
контрактом схемы являются миграции библиотеки, а не ORM-классы.

## PgBouncer

Обычные операции поддерживают transaction pooling: каждая операция получает
короткую транзакцию из SQLAlchemy pool. Для asyncpg отключите кэш prepared
statements, если соединение идёт через PgBouncer в transaction mode:

```python
engine = create_async_engine(
    DATABASE_URL,
    connect_args={"prepared_statement_cache_size": 0},
    pool_pre_ping=True,
)
```

`LISTEN/NOTIFY` требует закреплённого соединения и не совместим с transaction
pooling. Для процесса `dispatcher` используйте прямой URL PostgreSQL. Если
прямое соединение недоступно, диспетчер продолжит работать polling-циклами с
большей задержкой. Миграции также рекомендуется выполнять напрямую.

## Dishka

`IdentityHub` — зависимость уровня приложения: его реестр сервисов, dispatcher и
store должны быть общими для обработчиков процесса. В Dishka это `Scope.APP`.
Пример ниже использует асинхронный контейнер и освобождает engine при закрытии
APP scope:

```python
from collections.abc import AsyncIterable

from dishka import Provider, Scope, make_async_container, provide
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from identity_hub.crypto import EnvSecretsProvider
from identity_hub.hub import IdentityHub


class IdentityProvider(Provider):
    scope = Scope.APP

    @provide
    async def engine(self) -> AsyncIterable[AsyncEngine]:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        yield engine
        await engine.dispose()

    @provide
    def sessions(self, engine: AsyncEngine) -> async_sessionmaker:
        return async_sessionmaker(engine, expire_on_commit=False)

    @provide
    async def hub(self, sessions: async_sessionmaker) -> IdentityHub:
        hub = IdentityHub(
            sessionmaker=sessions,
            secrets=EnvSecretsProvider(),
            services=SERVICES,
        )
        await hub.start()
        return hub


container = make_async_container(IdentityProvider())
hub = await container.get(IdentityHub)
```

Закройте контейнер при остановке приложения. Для request scope внедряйте уже
созданный `IdentityHub` или `SpaceHandle`; не создавайте новый hub и engine на
каждый HTTP-запрос или задачу.

## Жизненный цикл процессов

Минимальная production-топология состоит из миграционного job, прикладных
воркеров и хотя бы одного dispatcher. Отдельный maintenance-процесс полезен,
если dispatcher не работает постоянно.

`hub.start()` сверяет ревизию схемы (`ih_alembic_version`) с той, что ожидает код, и при
несовпадении — более старой, более новой, отсутствующей или разветвлённой — бросает
`SchemaIncompatible` с ожидаемой и фактической ревизией, без URL и SQL. Миграции
`start()` не применяет: сначала `identity-hub migrate`, затем выкатка кода. `MemoryStore`
всегда совместим.

```text
migration job -> PostgreSQL schema
web/worker     -> hub.start() -> register/acquire/operation
dispatcher     -> events/webhooks/notifications + due maintenance
maintenance    -> timers/retention (опционально отдельно)
```

Встроить dispatcher в один процесс можно через контекстный менеджер:

```python
await hub.start()
async with hub.dispatcher.running():
    await serve_application(hub)
```

Для независимого процесса задайте `IDENTITY_HUB_FACTORY` и запустите
`identity-hub dispatcher`. При graceful shutdown сначала прекратите принимать
новую работу, выйдите из активных аренд, остановите dispatcher, затем вызовите
`await engine.dispose()`.

Пользовательская транзакция подключается явно:

```python
async with sessions() as db_session, db_session.begin():
    await hub.space("production").using(db_session).identities.create(IdentityCreate(...))
    await application_repository.save(db_session, ...)
```

Сессия должна принадлежать тому же engine. После выхода из транзакции не
сохраняйте объект `using(db_session)`: обычный `SpaceHandle` безопасно переиспользовать.
