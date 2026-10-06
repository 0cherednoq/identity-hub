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

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, hide_parameters=True)
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

Runtime-роли достаточно прав на объекты `ih_*` в выбранной схеме. Роли мигратора нужно:

| Что | Когда право нужно |
|---|---|
| `CREATE` на базу | схемы `db_schema` ещё нет или расширение `pg_trgm` не установлено |
| владение схемой `db_schema` (или `CREATE` в ней) | всегда: миграции создают и меняют таблицы |
| `USAGE` на схему, где стоит `pg_trgm` | всегда |

Без права `CREATE` на базу миграции работают, если DBA заранее выполнил:

```sql
CREATE SCHEMA identity_hub AUTHORIZATION migrator;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

`pg_trgm` может стоять в любой схеме, в том числе вне `search_path` (в Supabase это
`extensions`): `migrate` находит её в `pg_extension`. Имя `db_schema` — обычный идентификатор:
латинские буквы, цифры и подчёркивание, до 63 символов; иначе `ValidationFailed`.

Запускайте миграции через прямое подключение к PostgreSQL или пул в режиме сессии. Блокировка
от параллельного запуска держится на сессии, поэтому PgBouncer в режиме транзакций для
`migrate` не подходит.

Каждая ревизия применяется отдельной транзакцией. Если ревизия остановилась с ошибкой,
предыдущие уже применены; после исправления причины запустите `migrate` снова — он продолжит
с остановившейся ревизии.

## Read-схема: join с таблицами приложения

`hub.read` — публичная read-схема на SQLAlchemy Core (версия `hub.read.version`). Она даёт
стабильные selectable без секретов и служебных полей: `identities`, `leases`,
`lease_history`, `region_states`, `events`, `proxies`. Физические таблицы библиотеки приватны и
могут меняться между версиями; read-схема меняется только с повышением версии.

```python
from sqlalchemy import select

identities = hub.read.identities
ref = await space.ref()
stmt = (
    select(FarmAccount.identity_id)
    .join(identities, identities.c.id == FarmAccount.identity_id)
    .where(identities.c.space_id == ref.id, identities.c.standing == "good", FarmAccount.energy >= 500)
)
rows = (await db_session.execute(stmt)).scalars().all()        # обычный запрос приложения

farmers = space.pools.define("farmers", service=GAME)
async with farmers.lease(among=stmt) as lease:                # тот же select как отбор кандидатов
    ...
```

- Каждая таблица содержит `space_id` — фильтруйте по `(await space.ref()).id`.
- Имена колонок совпадают со словарём API: `identities.c.auth`, `identities.c.revision`,
  `proxies.c.group`. `READ_SCHEMA_VERSION = 4`: с версии 2 `identities.c.revision` — ревизия
  агрегата, а не номер события (`D-205`), у `events` появилась колонка `revision`. С версии 3 у `leases` и `lease_history` есть колонки `purpose` и `ref` — назначение аренды, с версии 4 — `hold_deadline`, потолок удержания. Секретов (`credentials`, сессии, логины прокси), `token` аренды, fencing и
  fingerprint в схеме нет.
- Только чтение: изменения — через `space.*`.
- **Отдельная схема.** При `IdentityHub(db_schema="identity_hub")` read-таблицы получают эту
  схему явно, поэтому запросы приложения не требуют `schema_translate_map`. Если такой select
  передаётся в `among=`, библиотека выполняет его со своей картой `{None: db_schema}`: таблицы
  приложения в нём должны иметь явную схему (`Table(..., schema="public")` или
  `__table_args__ = {"schema": "public"}`), иначе они будут искаться в схеме библиотеки.
- `among=Select` работает только с PostgreSQL; `MemoryStore` принимает список id.

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

## Ошибки базы данных и логи

Библиотека не выпускает наружу исключения драйвера: ошибка БД превращается в `DatabaseBusy`
(lock/statement timeout, deadlock, конфликт сериализации — повторите позже) или `StorageError`
(всё остальное). В сообщении и `context` остаётся только SQLSTATE; SQL, параметры запроса и
исходное исключение не сохраняются. Причина: среди параметров есть `token` аренды и шифртексты.

Тот же engine использует и ваш код, поэтому создавайте его с `hide_parameters=True`: тогда
SQLAlchemy не допишет `[parameters: ...]` к тексту ошибок ваших собственных запросов и к
логу `sqlalchemy.engine`. Не включайте `echo=True` в продакшене: он печатает параметры всех
запросов, включая запросы библиотеки.

## PgBouncer

Обычные операции поддерживают transaction pooling: каждая операция получает
короткую транзакцию из SQLAlchemy pool, а настройки сессии (`set_config(…, true)`,
`pg_advisory_xact_lock`) живут только внутри транзакции. asyncpg при этом должен не
полагаться на prepared statements соединения: в режиме транзакций следующий запрос может
уйти на другое серверное соединение, где такого statement нет
(`prepared statement "__asyncpg_stmt_N__" does not exist`). Нужны все три настройки:

```python
from uuid import uuid4

engine = create_async_engine(
    DATABASE_URL,  # адрес PgBouncer
    connect_args={
        "statement_cache_size": 0,  # кэш asyncpg
        "prepared_statement_cache_size": 0,  # кэш диалекта SQLAlchemy
        # уникальные имена: неименованные statement asyncpg PgBouncer путает между клиентами
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    },
    pool_pre_ping=True,
)
```

Рецепт проверен на PgBouncer 1.26 в режиме `transaction` и с `max_prepared_statements = 0`, и с
настройкой по умолчанию; в CI его проверяет job `pgbouncer`. Начиная с PgBouncer 1.21 prepared
statements в этом режиме может поддерживать сам PgBouncer (`max_prepared_statements` больше 0) —
рецепт с этим совместим. Одной настройки `prepared_statement_cache_size: 0` недостаточно: под
конкурентной нагрузкой с ней падали от 19 до 26 операций из 60.

`LISTEN/NOTIFY` требует закреплённого соединения и через PgBouncer в режиме транзакций не
работает. Диспетчер это обнаруживает сам: при подписке он отправляет пробное уведомление с
другого соединения, и если оно не дошло за 5 секунд, возвращает соединение в пул и пишет в лог
`identity_hub` (уровень INFO) «LISTEN недоступен: …; диспетчер работает в режиме опроса». В
режиме опроса события доставляются с задержкой до `poll_interval`. Чтобы события будили
диспетчер сразу, дайте процессу `dispatcher` прямой URL PostgreSQL.

Миграции (`identity-hub migrate`) запускайте только по прямому адресу или через пул в режиме
сессии: их блокировка от параллельного запуска сессионная.

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

Обслуживание устойчиво к отдельным сбоям. Аккаунт, чей таймер не удалось применить,
пропускается с предупреждением в логе и счётчиком
`identity_hub_failures_total{scope="maintenance"}`; остальные аккаунты и группы задач
(таймеры, аренды, очистка) выполняются. Аккаунты сервиса, которого нет в `services` этого
процесса (сервис убрали из кода, а данные остались), пропускаются с `reason="unknown_service"` —
верните сервис в конфигурацию или удалите его аккаунты.

`hub.start()` сверяет ревизию схемы (`ih_alembic_version`) с множеством ревизий, на которых
работает код (`identity_hub.postgres.migrate.SCHEMA_COMPATIBLE`; последняя из них —
`SCHEMA_HEAD`). Если ревизия в него не входит — слишком старая, более новая, отсутствует или
разветвлена — `start()` бросает `SchemaIncompatible` с ожидаемой и фактической ревизией и
подсказкой, что делать (`identity-hub migrate` или обновление пакета), без URL и SQL. Миграции
`start()` не применяет. `MemoryStore` всегда совместим.

### Обновление без простоя

Версия библиотеки запускается на схеме любой ревизии из своего `SCHEMA_COMPATIBLE`, поэтому
обновление идёт так:

1. Проверьте, что текущая ревизия базы входит в `SCHEMA_COMPATIBLE` новой версии. Если нет —
   обновление требует остановки: остановите процессы, выполните `identity-hub migrate`,
   запустите новую версию.
2. Выкатите новый код на все процессы. Он работает на прежней схеме; процессы со старым кодом
   в это время перезапускаются без ошибок, потому что схема ещё не менялась.
3. Когда старого кода не осталось, выполните `identity-hub migrate`.

Не запускайте `migrate` до конца шага 2: старый код о новой ревизии не знает и после перезапуска
не стартует. Миграции библиотеки пишутся по правилу expand/contract: ревизия либо только
добавляет (таблицу, колонку со значением по умолчанию, индекс), и тогда предыдущая ревизия
остаётся совместимой, либо удаляет то, чем код перестал пользоваться в предыдущем выпуске.
Выпуск, которому без новой ревизии работать нельзя, сужает `SCHEMA_COMPATIBLE`; это отмечается
в CHANGELOG.

```text
migration job -> PostgreSQL schema
web/worker     -> hub.start() -> register/acquire/operation
dispatcher     -> events/webhooks/notifications + due maintenance
maintenance    -> timers/retention (опционально отдельно)
```

Процесс с dispatcher отправляет запросы на адреса, которые задаёт оператор пространства
(вебхуки). Библиотека проверяет адрес назначения при каждом соединении, но надёжнее держать
dispatcher в сети без доступа к внутренним сервисам и metadata-адресу облака либо выпускать его
через egress-прокси (`WebhookPolicy(proxy=...)`) со своим списком разрешённых назначений.

Встроить dispatcher в один процесс можно через контекстный менеджер:

```python
await hub.start()
async with hub.dispatcher.running():
    await serve_application(hub)
```

Для независимого процесса задайте `IDENTITY_HUB_FACTORY` и запустите
`identity-hub dispatcher`. При graceful shutdown сначала прекратите принимать
новую работу, выйдите из активных аренд, остановите dispatcher, затем вызовите
`await hub.aclose()` и `await engine.dispose()`.

`hub.aclose()` записывает то, что хаб держит в памяти — телеметрию операций и счётчики
потерянных аренд, — и закрывает HTTP-клиент Telegram, если хаб создал его сам. Транспорт,
переданный параметром `telegram_transport`, и engine закрывает вызывающий. Повторный вызов
безопасен. То же делает контекстный менеджер, он же вызывает `start()`:

```python
async with hub:
    async with hub.dispatcher.running():
        await serve_application(hub)
await engine.dispose()
```

Телеметрию операций (`telemetry.*` в rollups) записывает dispatcher. В процессе без
dispatcher она копится в памяти: вызывайте `await hub.flush_metrics()` периодически и
`await hub.aclose()` при остановке. Буфер ограничен 10 000 ключей — при переполнении
отбрасываются самые старые минуты, а счётчик
`identity_hub_failures_total{scope="metrics_buffer"}` растёт.

Rollups по событиям пересчитываются только для затронутых минут; часовые и суточные корзины
собираются из минутных. `identity-hub rollups --rebuild` перестраивает диапазон по журналу
событий и не трогает телеметрию. Диапазон старше срока хранения событий (`Retention.events`)
по журналу уже не восстановить — команда откажет; `--force` перестроит его, стерев историю.

Пользовательская транзакция подключается явно:

```python
async with sessions() as db_session, db_session.begin():
    await hub.space("production").using(db_session).identities.create(IdentityCreate(...), actor="operator")
    # пространство "production" создано заранее: hub.spaces.ensure("production", "Production")
    await application_repository.save(db_session, ...)
```

Сессия должна принадлежать тому же engine. После выхода из транзакции не
сохраняйте объект `using(db_session)`: обычный `SpaceHandle` безопасно переиспользовать.
