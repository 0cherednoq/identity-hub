# Карта API

Справочник по публичному API: с чего начать, где какая сущность и какие у неё операции.
Подробности и сценарии — в разделах руководства, ссылки в каждой таблице.

Термин: **identity**, или аккаунт, — учётная запись во внешнем сервисе (Telegram, почта, биржа).

## Точка входа

```mermaid
flowchart LR
    S["Service<br/>(объявление в коде)"] --> H["IdentityHub(...)"]
    H -->|"await hub.start()"| H2["хаб готов"]
    H2 -->|"hub.space(slug)"| SP["SpaceHandle"]
    SP --> I["space.identities"]
    SP --> PO["space.pools → Pool → Lease"]
    SP --> PR["space.proxies"]
    SP --> O["space.auth / sync / verifications<br/>links / data / secrets / imports"]
    SP --> AD["space.admin / analytics / maintenance"]
    SP --> EV["space.webhooks / notifications"]
```

```python
from identity_hub import EnvSecretsProvider, IdentityHub

hub = IdentityHub(sessionmaker=sessions, secrets=EnvSecretsProvider(), services=[TELEGRAM])
await hub.start()                       # синхронизирует сервисы и проверяет схему БД
space = await hub.spaces.ensure("production", "Production")  # пространство: изоляция данных
```

Один `IdentityHub` на процесс. Всё остальное — через пространство `space`: у него нет плоских
методов, только **неймспейсы** по сущностям.

| Неймспейс | Сущность | Ключевые методы | Раздел |
|---|---|---|---|
| `space.identities` | identity | `get`, `find`, `get_by_ref`, `card`, `search`, `list`, `count`, `create`, `ensure`, `create_many`, `update`, `replace_*`, `add_tags`, `remove_tags`, `set_tags`, `set_note`, `delete`, `activate`, `retire`, `set_lifecycle`, `restore_standing`, `override`, `unpin` | [Аккаунты](03-accounts.md), [Состояния](04-states.md) |
| `space.imports` | импорт | `run`, `run_files`, `stage`, `get`, `cancel` | [Аккаунты](03-accounts.md#импорт) |
| `space.secrets` | credentials, сессии | `describe`, `reveal`, `replace_credentials`, `replace_session` | [Аккаунты](03-accounts.md#секреты) |
| `space.pools` | пул и аренда | `define` → `Pool.lease()` / `Pool.acquire()`; `revoke` | [Аренда](07-leases.md) |
| `space.proxies` | прокси | `get`, `list`, `url`, `create`, `ensure`, `create_many`, `replace_credentials`, `move`, `delete`, `report_failure`, `report_success`, `recheck`, `recheck_group`, `recheck_due`, `override`, `rebind` | [Прокси](09-proxies.md) |
| `space.auth` | вход | `login`, `login_many`, `answer_challenge` | [Вход](05-login.md) |
| `space.sync` | сверка с платформой | `refresh`, `refresh_many` | [Состояния](04-states.md#синхронизация-статусов-с-платформой) |
| `space.verifications` | верификация | `status`, `review`, `requirements.add`, `requirements.resolve` | [Верификация](06-verification.md) |
| `space.links` | связи identity | `add`, `remove`, `of`, `impact` | [Аккаунты](03-accounts.md#связи-между-аккаунтами) |
| `space.data` | данные приложения | `load`, `save` | [Аккаунты](03-accounts.md#свои-данные-домена) |
| `space.webhooks`, `space.notifications` | доставки | `create`, `create_rule`, `send_test`… | [События](10-events.md) |
| `space.admin` | read-модели UI | `overview`, `identity_page`, `lease_dashboard`, `event_page`, `human_queue`… | для админок |
| `space.analytics` | отчёты | `series`, `funnel`, `cohorts`, `ban_rate`, `utilization`… | для админок |
| `hub.read` | read-схема Core | `identities`, `leases`, `lease_history`, `region_states`, `events`, `proxies`, `proxy_bindings` — для join и `among=` | [Интеграция](../integrations.md#read-схема-join-с-таблицами-приложения) |
| `space.maintenance` | обслуживание | `expire_challenges`, `expire_verifications`, `reindex_search` | обычно вызывает диспетчер |

## Контракт хаба

Публичные члены `IdentityHub` перечислены ниже полностью. Всё остальное — имена с `_`:
хранилище, шифрование и служебные объекты диспетчера не входят в контракт и могут меняться
без предупреждения. Расшифровать секрет в обход аренды и аудита через хаб нельзя.

| Член | Назначение |
|---|---|
| `hub.start()` | проверить ревизию схемы и синхронизировать реестр сервисов |
| `hub.aclose()`, `async with hub:` | штатное завершение: записать накопленные метрики и потери аренд, закрыть HTTP-клиент, созданный хабом |
| `hub.register_service(service)`, `hub.service(name)`, `hub.services` | реестр сервисов |
| `hub.spaces.create(slug, title)`, `hub.spaces.ensure(slug, title)`, `hub.spaces.get(slug)` | создать пространство (уже есть — `AlreadyExists`), создать при необходимости, найти существующее (`NotFound`) |
| `hub.space(slug)`, `hub.get_space(slug)` | пространство по slug без обращения к БД; несуществующее — `NotFound` при первой операции (`create=True` — создать); `get_space` возвращает `None` |
| `hub.list_spaces()`, `hub.spaces_matrix()`, `hub.service_catalog()`, `hub.background_snapshot()` | сводные read-модели по пространствам для админок |
| `hub.read` | read-схема Core для join и `among=` |
| `hub.events` | `subscribe(name, fn, types=...)` — подписчики в коде, см. [События](10-events.md) |
| `hub.dispatcher` | фоновый цикл: `running()`, `run_once()`, `drain()` |
| `hub.maintenance` | обслуживание по расписанию: таймеры и retention |
| `hub.doctor` | диагностика и починка данных (`identity-hub doctor`) |
| `hub.equivalence_tag(value)` | HMAC-тег значения ключом отпечатков: сравнить чувствительные данные, не храня их (`D-208`) |
| `hub.metrics` | хук метрик, заданный параметром `metrics` |
| `hub.lease_log_sink` | обработчик `logging` для записи логов аренды в `ih_lease_logs` |
| `hub.flush_lease_losses()` | выпустить накопленные события `worker.leases_lost` |
| `hub.flush_metrics()` | записать накопленную телеметрию операций в rollups; нужен процессу без dispatcher |
| `hub.sessionmaker` | фабрика сессий PostgreSQL для своей транзакции и `space.using(session)`; `None` для другого хранилища |
| `hub.clock`, `hub.retention`, `hub.timezone`, `hub.admin_base_url`, `hub.db_schema` | настройки конструктора, только для чтения |
| `hub.proxy_health`, `hub.webhook_policy`, `hub.webhook_dev_hosts` | политики прокси и доставки |
| `hub.strict_events`, `hub.redact_refs_in_events` | режимы очистки событий |

## Что входит в публичный контракт

Версия `1.0.0aN` — альфа: контракт ниже может меняться между альфа-выпусками, каждое
несовместимое изменение отмечается в CHANGELOG пометкой **Несовместимо**. Контракт — это:

| Часть | Где описано |
|---|---|
| Имена из корневого `identity_hub.__all__` (классы, DTO, перечисления, ошибки, протоколы адаптеров) | эта страница, [Сервисы](02-services.md) |
| Неймспейсы пространства (`space.identities`, `space.pools`, …), `space.using`, `space.ref` и члены `IdentityHub` из раздела «Контракт хаба» | эта страница |
| Типы событий и их поля `data` | [События](10-events.md) |
| Коды ошибок `IdentityHubError.code`, `context`, `http_status()` и `exit_code()` | модуль `identity_hub.errors`, коды выхода — [CLI](../cli.md#диагностика-cli) |
| Read-схема `hub.read` и её версия `READ_SCHEMA_VERSION` | [Интеграции](../integrations.md#read-схема-join-с-таблицами-приложения) |
| Формат шифртекста в БД (envelope AES-256-GCM, AAD) и ротация ключей | [Секреты](03-accounts.md#секреты), [`rotate-key`](../cli.md#rotate-key) |
| Команды и коды выхода CLI `identity-hub` | [CLI](../cli.md) |
| Совместимость кода и схемы БД (`SCHEMA_COMPATIBLE`) | [Обновление без простоя](../integrations.md#обновление-без-простоя) |

Всё остальное внутреннее и меняется без предупреждения: модули и имена с `_`, подмодули, не
реэкспортированные из корня, таблицы `ih_*` (кроме read-схемы), тексты сообщений и логов.
Контракт `Store` (`identity_hub.store`) и `StoreConformanceSuite` — внутренние и нестабильные
до 1.0: своё хранилище писать можно, но его придётся обновлять вместе с библиотекой.

Сообщения ошибок, логи и документация — на русском; идентификаторы, коды ошибок, типы событий
и поля — на английском, их можно разбирать программно.

## Значения по умолчанию

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `LeaseTiming.ttl` | 1 мин | срок аренды без heartbeat |
| `LeaseTiming.heartbeat` | 20 с | период продления аренды (не больше TTL/3) |
| `LeaseTiming.safety_margin` | 20 с | запас до истечения, после которого клиент считает аренду потерянной |
| `LeaseTiming.stats_flush` | 5 мин | как часто heartbeat пишет в историю время и счётчики операций |
| `Service.login_timeout` | 2 мин | предел одного вызова `Authenticator.login` |
| `Service.probe_timeout` | 1 мин | предел одного вызова пробы |
| `LoginChallenge.expires_in` | 1 ч | срок вопроса платформы, если `expires_in` не задан |
| `ProxyHealthPolicy.error_threshold` | 3.0 | счёт ошибок прокси до карантина |
| `ProxyHealthPolicy.check_timeout` | 15 с | предел одной проверки прокси |
| `ProxyHealthPolicy.max_backoff` | 1 ч | наибольшая пауза между перепроверками |
| `WebhookPolicy.timeout` | 10 с | предел одного HTTP-запроса вебхука |
| `WebhookPolicy.max_failures` | 10 | неудач подряд до отключения endpoint |
| `LeaseLogPolicy.max_per_lease` | 200 | записей лога на одну аренду |
| `MaintenanceSchedule.normalize_seconds` | 1 мин | период таймеров состояний |
| `Retention.events` | 180 дн | хранение событий |
| `Retention.lease_history` | 90 дн | хранение истории аренд |
| `Retention.operation_keys` | 7 дн | хранение ключей идемпотентности операций |
| `identities.create_many` | 1000 | записей в одном атомарном вызове |
| `imports.stage` | 10000 | записей в одном фоновом импорте |
| `limit` | 500 | наибольшая страница `identities.list`/`search` (прокси — 200) |
| `data.save` | 64 КиБ | JSON записи данных приложения |
| `MAX_SECRET_BYTES` | 512 КиБ | открытый текст одного секрета |
| `MAX_ENVELOPE_BYTES` | 1 МиБ | шифртекст одного секрета |

Остальные сроки хранения — поля `Retention`.

## Конвенция глаголов

| Метод | Что делает |
|---|---|
| `get(id)` | объект или `NotFound` |
| `find(...)` | объект или `None` |
| `list(filter)` / `search(query)` | страница `Page[T]`: `items`, `next_cursor`, `total` (при `count=True`; точно до 1000, больше — 1001) |
| `create(spec)` | создать; уже есть — `AlreadyExists` |
| `ensure(spec)` | вернуть существующий без изменений или создать → `(obj, created)` |
| `create_many(specs, on_conflict=)` | массовое создание одной транзакцией: `"fail"` или `"skip"` |
| `update(id, ...)` | частичное изменение со слиянием |
| `replace_<часть>(id, ...)` | полная замена части |
| `delete(id)` | физическое удаление |
| `add` / `remove` | только для членства: теги, связи |

Объекты чтения — лёгкие DTO без секретов: `Identity`, `Proxy`. Тяжёлая карточка со
состоянием, квотами, арендой и блокерами — `identities.card(id)`.

## Типовые операции по сущностям

### Сервис

```python
TELEGRAM = Service(
    name="telegram", title="Telegram",
    credentials=TgCredentials, session=TgSession,
    operations={"send_message": OperationRule(quotas=(Quota(20, timedelta(hours=1)),))},
)
hub = IdentityHub(..., services=[TELEGRAM])     # или hub.register_service(TELEGRAM) до start()
await hub.start()
hub.service("telegram")                         # объявление по имени
```

Подробно — [Сервисы](02-services.md).

### Identity (аккаунт)

```python
from identity_hub import IdentityCreate, IdentityFilter, Lifecycle

identity = await space.identities.create(IdentityCreate(
    service=TELEGRAM, external_ref="+12025550123",
    credentials={"phone": "+12025550123"}, session={"auth_key": "..."},
    tags=["parser"], lifecycle=Lifecycle.READY,
), actor="operator")
identity, created = await space.identities.ensure(IdentityCreate(...), actor="operator")   # get-or-create
result = await space.identities.create_many([...], on_conflict="skip", actor="operator")   # атомарно, до 1000

identity = await space.identities.get(identity_id)
identity = await space.identities.get_by_ref("telegram", "+12025550123")
page = await space.identities.list(IdentityFilter(service="telegram", standing="banned"))
page = await space.identities.search("tag:parser farm:eu-1")
card = await space.identities.card(identity_id)        # состояние, квоты, аренда, blockers

await space.identities.update(identity_id, profile={"city": "Berlin"}, labels={"farm": "eu-2"}, actor="operator")
await space.identities.add_tags(identity_id, ["vip"], actor="operator")
await space.identities.delete(identity_id, actor="operator")
```

### Состояние identity

| Задача | Метод |
|---|---|
| ввести в работу | `identities.activate(id, actor=...)` |
| вывести из работы | `identities.retire(id, actor=..., reason=...)` |
| произвольный lifecycle | `identities.set_lifecycle(id, state, actor=...)` |
| разбанить | `identities.restore_standing(id, actor=..., reason=...)` |
| забанить, заморозить, снять карантин | `identities.override(id, Override(region, key, state, reason=..., actor=...))` |
| закрепить статус от проб | `Override(..., pin_for=timedelta(hours=6))`, снять — `identities.unpin(...)` |
| узнать статус у платформы | `sync.refresh(id)`, по расписанию — `sync.refresh_many(...)` |
| автоматически по ошибкам платформы | `Service(error_classifier=...)` |

Подробно — [Состояния](04-states.md).

### Аренда

```python
pool = space.pools.define("senders", service=TELEGRAM, tags=["sender"])
async with pool.lease(for_operation="send_message", wait=30) as lease:
    session = await lease.session()
    async with lease.operation("send_message", key=f"send:{job.id}"):
        await client.send(...)
await space.pools.revoke(identity_id, reason="зависший воркер", actor="admin")
```

Подробно — [Аренда](07-leases.md).

### Лимиты на действия

| Лимит | Где | Как |
|---|---|---|
| квота N за окно | сервис | `OperationRule(quotas=(Quota(20, timedelta(hours=1)),))` |
| допуск по статусу и верификации | сервис | `OperationRule(standing=..., needs_auth=..., verified=..., tiers=...)` |
| отдых после серии операций | пул | `pools.define(..., pacing=Pacing(work=Quota(...), pause=(15, 40)))` |
| пауза по требованию платформы | операция | `await op.cool_down(seconds)` или `Fault(retry_after=...)` |
| карантин за ошибки | сервис | `quarantine_threshold` + `ErrorClassifier` |

Подробно — [Лимиты](08-limits.md).

### Прокси

```python
proxy, created = await space.proxies.ensure("socks5://user:pass@1.2.3.4:1080", group="residential")
result = await space.proxies.create_many(urls, group="residential", on_conflict="skip")
page = await space.proxies.list(group="residential", status="alive")
TELEGRAM = Service(..., proxy_policy=ProxyPolicy(group="residential", sticky=True))
await space.proxies.recheck_due(checker)
```

Подробно — [Прокси](09-proxies.md).

### Пакетные команды

Команды над множеством (`identities.activate_many`, `retire_many`, `restore_standing_many`,
`delete_many`; `proxies.delete_many`, `override_many`, `set_enabled_many`, `rebind_many`) возвращают
`BatchResult`: `applied` — ключи применённых сущностей, `rejected` — список `BatchRejection`
(`key`, `code`, `message`). Пачка — одна транзакция, до 1000 ключей; ключи блокируются по
возрастанию. Параметр `atomic=True` отменяет пачку при первом отказе.

### Вход, верификация, связи, данные, секреты, импорт

```python
outcome = await space.auth.login(identity_id)             # LoginOutcome.SUCCEEDED / CHALLENGED / FAILED
await space.auth.answer_challenge(challenge_id, "12345", actor="operator")

await space.verifications.review(identity_id, "kyc_id", "approve", actor="admin", reason="ok")

await space.links.add(tg_id, "recovery_email", mail_id, actor="operator")
record = await space.data.load(identity_id, "farm.warmup", Warmup)

creds = await space.secrets.reveal(identity_id, SecretRef.CREDENTIALS, actor="admin", reason="...")
report = await space.imports.run(service=TELEGRAM, records=records, actor="operator")
```
