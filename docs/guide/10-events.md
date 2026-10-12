# 10. События и уведомления

Каждое значимое изменение пишется в журнал событий в той же транзакции, что и само изменение.
Журнал — источник для подписчиков в коде, вебхуков, Telegram-уведомлений и аналитики.
Доставка — **at-least-once**: обработчик может получить событие повторно, дедуплицируйте по
`event.event_id`.

```mermaid
flowchart LR
    CH["изменение<br/>(register, бан, аренда…)"] -->|одна транзакция| J[("журнал ih_events")]
    J --> D["Dispatcher"]
    D --> CB["ваши подписчики<br/>hub.events.subscribe"]
    D --> WH["вебхуки<br/>HTTP POST + HMAC"]
    D --> TG["Telegram-бот<br/>правила, дайджесты"]
    D --> AN["аналитика, rollups"]
```

**Без запущенного диспетчера события копятся, но никуда не доставляются**, а таймеры состояний
не срабатывают. Запуск: `async with hub.dispatcher.running(): ...` внутри приложения или
`identity-hub dispatcher` отдельным процессом ([CLI](../cli.md)).

## Событие

`Event`: `event_id`, `type`, `severity` (`info` / `warning` / `critical`), `occurred_at`,
`space`, `service`, `identity_id`, `proxy_id`, `lease_id`, `sequence`, `revision`, `actor`, `data`.

- `sequence` — номер события аккаунта: растёт на каждое событие с `identity_id`, в том числе от
  аренды. По нему восстанавливается порядок событий одного аккаунта.
- `revision` — ревизия аккаунта после команды, которая записала событие (`D-205`). Ревизия растёт
  на 1 за команду, изменившую аккаунт: создание, правка тегов, меток, профиля, заметки,
  lifecycle, регионов, связей, требований, верификации, credentials и сессий. Все события одной
  команды несут одну ревизию. Захват и освобождение аренды, операции, прокси, раскрытие секрета
  (`identity.secret.revealed`), исчерпание квоты и `identity.state.drift` ревизию не меняют.
  У событий без аккаунта и у событий, записанных до миграции `0017`, — `None`.

Секреты в `data` не попадают. На `[redacted]` заменяются:

- значения полей, чьё имя называет секрет: `password`, `bot_token`, `api_key`, `auth_key`,
  `authorization`, `cookie`, `credentials`, `evidence`, `jwt`, `otp`, `passphrase`, `proxy`,
  `secret`, `totp` и подобные. Сравнивается главное слово имени, поэтому `tokens_used`,
  `password_changed_at` и `proxy_id` остаются как есть;
- `SecretStr`, `SecretValue`, `bytes` и pydantic-модели;
- учётные данные внутри строк, в том числе в списках: `scheme://user:pass@host`, query-строка
  URL, `Bearer …`, `password=…`.

Объект неизвестного типа заменяется именем типа (`<MyContext>`). С
`IdentityHub(strict_events=True)` вместо вырезания бросается `SecretLeak` — удобно в тестах.

Текст, который приходит из вашего кода и от платформы, перед записью укорачивается и проходит
ту же очистку: `raw_status` (до 128 символов) и `code` (до 64) из `Fault` и `ProbeResult`,
`hint` из `LoginChallenge` (до 256), причины команд и override (до 1000). Текст исключения,
упавшего в теле аренды или операции, не сохраняется вовсе: в `last_error` истории аренды и в
`data.error` событий `lease.*` и `operation.failed` остаётся только имя типа исключения.

**Персональные данные.** `identity.registered` и `identity.deleted` несут `external_ref`
(телефон, email) и `labels` — это часть контракта событий: по ним ваша система сопоставляет
аккаунт. Если события уходят во внешние вебхуки и такие данные там недопустимы, создайте хаб с
`IdentityHub(redact_refs_in_events=True)`: `external_ref`, `normalized_ref` и `labels` во всех
событиях заменяются на `[redacted]`, аккаунт остаётся опознаваемым по `identity_id`.

## Основные типы

| Группа | События | Когда полезно |
|---|---|---|
| Аккаунт | `identity.registered`, `imported`, `updated`, `deleted`, `lifecycle.changed` | Синхронизация с вашей CRM |
| Состояния | `identity.standing.changed`, `auth.changed`, `availability.changed`, `verification.changed`, `tier.reached/lost`, `state.overridden`, `state.drift` | Реакция на баны, разлогины, карантин |
| Вход | `identity.login.succeeded/failed`, `identity.challenge.opened/answered/expired` | Показать оператору, что нужен SMS-код |
| Секреты | `identity.credentials.updated`, `session.updated`, `secret.revealed` | Аудит |
| Прокси | `proxy.status.changed`, `proxy.binding.changed`, `proxy.pool.exhausted`, `proxy.checker.failed`, `proxy.disabled`, `proxy.enabled`, `proxy.updated`, `proxy.deleted`, `proxy.secret.revealed` | Мониторинг прокси, аудит |
| Пулы | `pool.capacity.low/empty`, `identity.quota.exhausted`, `worker.leases_lost` | Нехватка аккаунтов, проблемы воркеров |

События аренд и операций (`lease.*`, `operation.*`) и `dispatcher.event.skipped` — журнальные: они видны в
`space.admin.event_page(filters=EventFilters(include_journal=True))`, но подписчикам и вебхукам не
отправляются. Своих типов событий добавить нельзя.

## Состав `data`

Ключи `data` по типам событий. Ключ из таблицы может отсутствовать в конкретном событии
(например, `reason` есть только у команд с причиной), но других ключей в `data` нет: таблицу
сверяет тест на каждом прогоне. Значения — строки, числа, логические и `null`; идентификаторы
и время приходят строками (UUID и ISO 8601). Журнальные типы (`lease.*`, `operation.*`,
`session.saved`, `dispatcher.event.skipped`) подписчикам не отправляются.

У событий смены состояния (`identity.*.changed`, `proxy.status.changed`) состав общий: `region`
и `key` — какой регион изменился, `from` и `to` — прежнее и новое состояние, `code` и `until` —
код и срок временного состояния, `source` — источник (`command`, `observation`, `error`, …),
`actor` и `reason` — кто и почему, `raw_status` — исходный статус платформы для наблюдений.

<!-- event-data:start -->
| Тип | Ключи `data` |
|---|---|
| `admin.accounts.exported` | `count`, `filter_fingerprint`, `format`, `reason` |
| `admin.bulk.executed` | `attempt`, `failed`, `operation`, `reason`, `snapshot_id`, `succeeded` |
| `admin.events.exported` | `count`, `filter_fingerprint`, `format`, `reason` |
| `admin.import.changed` | `action`, `count`, `reason`, `run_id`, `service` |
| `admin.integration.changed` | `action`, `outcome`, `reason`, `resource_id`, `resource_kind` |
| `dispatcher.event.skipped` | `attempts`, `consumer`, `error`, `event_id`, `type` |
| `identity.auth.changed` | `actor`, `code`, `from`, `key`, `raw_status`, `reason`, `region`, `source`, `to`, `until` |
| `identity.availability.changed` | `actor`, `code`, `from`, `key`, `raw_status`, `reason`, `region`, `source`, `to`, `until` |
| `identity.challenge.answered` | `challenge_id`, `kind` |
| `identity.challenge.expired` | `challenge_id`, `kind` |
| `identity.challenge.opened` | `challenge_id`, `expires_at`, `hint`, `kind` |
| `identity.credentials.updated` | `action`, `fields`, `kind`, `name`, `reason` |
| `identity.deleted` | `external_ref` |
| `identity.imported` | `count`, `run_id`, `skipped`, `updated` |
| `identity.lifecycle.changed` | `actor`, `code`, `from`, `key`, `raw_status`, `reason`, `region`, `source`, `to`, `until` |
| `identity.linked` | `linked_identity_id`, `reason`, `role` |
| `identity.login.failed` | `error` |
| `identity.login.succeeded` | — |
| `identity.quota.exhausted` | `kind`, `limit`, `resets_at`, `window_seconds`, `window_start` |
| `identity.registered` | `auth`, `external_ref`, `labels`, `lifecycle`, `links`, `tags` |
| `identity.requirement.added` | `code`, `deadline`, `since` |
| `identity.requirement.resolved` | `code`, `reason` |
| `identity.secret.revealed` | `field`, `kind`, `name`, `reason` |
| `identity.session.updated` | `expires_at`, `fields`, `mode`, `reason`, `replaced_id`, `session_id`, `session_revision`, `slot` |
| `identity.standing.changed` | `actor`, `code`, `from`, `key`, `raw_status`, `reason`, `region`, `source`, `to`, `until` |
| `identity.state.drift` | `cause`, `key`, `observed`, `raw_status`, `region`, `source`, `state` |
| `identity.state.overridden` | `actor`, `attempts`, `code`, `from`, `host`, `key`, `pinned_until`, `pool`, `port`, `reason`, `region`, `to` |
| `identity.state.unpinned` | `actor`, `key`, `reason`, `region`, `state` |
| `identity.tier.lost` | `factor`, `tier` |
| `identity.tier.reached` | `factor`, `tier` |
| `identity.unlinked` | `linked_identity_id`, `reason`, `role` |
| `identity.updated` | `fields`, `labels`, `tags` |
| `identity.verification.changed` | `actor`, `attempts`, `code`, `from`, `key`, `raw_status`, `reason`, `region`, `source`, `to`, `until` |
| `lease.acquired` | `fencing`, `holder`, `pool`, `shared` |
| `lease.expired` | `error`, `expired_at`, `fencing`, `holder`, `pool`, `reason` |
| `lease.failed` | `error`, `pool`, `reason` |
| `lease.lost` | `error`, `pool`, `reason` |
| `lease.released` | `error`, `pool`, `reason` |
| `lease.revoked` | `holder`, `pool`, `reason` |
| `operation.failed` | `blame`, `code`, `error`, `kind` |
| `operation.succeeded` | — |
| `pool.capacity.empty` | `available`, `pool`, `threshold` |
| `pool.capacity.low` | `available`, `pool`, `threshold` |
| `proxy.binding.changed` | `from_proxy_id`, `pool`, `reason`, `to_proxy_id` |
| `proxy.checker.failed` | `pool`, `proxies`, `reason` |
| `proxy.deleted` | `host`, `pool`, `port`, `proxy_id`, `unbound` |
| `proxy.disabled` | `host`, `pool`, `port`, `reason`, `unbound` |
| `proxy.enabled` | `host`, `pool`, `port` |
| `proxy.pool.exhausted` | `pool`, `proxy_pool` |
| `proxy.secret.revealed` | `reason` |
| `proxy.status.changed` | `actor`, `code`, `from`, `host`, `key`, `pool`, `port`, `raw_status`, `reason`, `region`, `source`, `to`, `until` |
| `proxy.updated` | `fields`, `from_host`, `from_pool`, `from_port`, `from_scheme`, `host`, `pool`, `port` |
| `session.saved` | `mode`, `replaced_id`, `session_id`, `session_revision`, `slot` |
| `webhook.endpoint.disabled` | `endpoint_id`, `failure_streak` |
| `worker.leases_lost` | `count`, `holder` |
<!-- event-data:end -->

## Подписка в коде

```python
from identity_hub import Event

async def on_ban(event: Event) -> None:
    if event.data.get("to") == "banned":
        await crm.mark_account_lost(event.identity_id, reason=event.data.get("code"))

hub.events.subscribe("crm-bans", on_ban, types={"identity.standing.changed"})
hub.events.subscribe("audit", write_audit, types={"identity.*", "proxy.*"})   # маски
```

- Имя подписчика — ключ курсора в БД. **Новый подписчик начинает с начала хранимого журнала**
  (по умолчанию 180 дней) — будьте готовы к пачке старых событий. Чтобы начать с текущего
  момента, передайте `start="now"`: курсор создаётся, когда диспетчер впервые обрабатывает
  этого подписчика, и встаёт на текущий горизонт журнала. История не приходит; события
  транзакций, открытых в этот момент, приходят после их фиксации. Если рядом шла долгая
  транзакция, могут прийти и события, зафиксированные чуть раньше, — обработчик всё равно
  должен быть идемпотентным. У подписчика, который уже работал, параметр ничего не меняет.
- Подписчики вызываются только в процессе, где работает диспетчер.
- События приходят из всех пространств хаба; оставить одно пространство и один сервис —
  `subscribe(..., space=space, service="telegram")`.

Приложению, которое переносит события в свои таблицы, удобнее получать их пачкой — один запрос
к своей БД на пачку вместо запроса на событие:

```python
async def mirror(events: list[Event]) -> None:
    accounts = await load_accounts({e.identity_id for e in events})     # один запрос на пачку
    await save_history(events, accounts)

hub.events.subscribe_batch("console", mirror, types={"identity.*"}, space=space)
```

- Пачка — до 100 событий в порядке журнала (`Dispatcher(batch_size=...)`).
- Если обработчик бросил исключение, пачка делится пополам, пока не останется одно событие.
  События до сбойного доставляются и не повторяются; сбойное приходит снова и после
  `max_attempts` неудач пропускается — так же, как у `subscribe`. При сбое части пачки
  приходят обработчику повторно, поэтому он должен быть идемпотентным.

### Гарантии доставки

- **At-least-once.** Событие считается доставленным, когда обработчик вернулся без исключения.
  Если процесс упал посреди обработки, событие придёт ещё раз — обработчик должен быть
  идемпотентным (ключ — `event.event_id`).
- **Сбой на одном событии.** Обработчик бросил исключение — это событие придёт снова на
  следующем круге диспетчера (по умолчанию через 2 с). События, обработанные до него, не
  повторяются; следующие ждут.
- **Лимит попыток.** После `max_attempts` неудач подряд (по умолчанию 5,
  `subscribe(..., max_attempts=...)`) событие пропускается, подписчик получает следующие.
  Пропуск виден: журнальное событие `dispatcher.event.skipped` (`consumer`, `event_id`, `type`,
  `attempts`, имя типа исключения), запись в логе уровня `ERROR` и счётчик
  `identity_hub_failures_total{scope="subscriber", reason="skipped"}`. Счётчик попыток живёт в
  памяти процесса: после перезапуска диспетчера отсчёт для текущего события начинается заново.
- **Изоляция.** Подписчики, вебхуки, уведомления и обслуживание не зависят друг от друга: сбой
  одного потребителя не останавливает остальных.
- **Порядок и долгие транзакции.** Диспетчер читает события ниже горизонта самой старой
  открытой транзакции в базе: так событие, закоммиченное позже, но с меньшим номером, не
  теряется. Обратная сторона — любая долгая транзакция в той же базе, в том числе чужая
  (отчёт, `pg_dump`, забытый `BEGIN`), задерживает доставку **всех** событий до своего
  завершения. Следите за `identity_hub_dispatcher_cursor_lag` и ограничивайте длительность
  транзакций (`idle_in_transaction_session_timeout`).

## Вебхуки

```python
from pydantic import SecretStr
from identity_hub import Severity

endpoint = await space.webhooks.create(
    url="https://crm.example.com/hooks/identity",
    secret=SecretStr("whsec_..."),
    event_types=["identity.standing.changed", "identity.challenge.*"],
    filter={"service": "telegram", "labels": {"farm": "eu-1"}},
    min_severity=Severity.WARNING,
)
await space.webhooks.send_test(endpoint.id)           # HTTP-код ответа
await space.webhooks.rotate_secret(endpoint.id, SecretStr("whsec_new"), overlap=timedelta(hours=1))
```

- Только `https` (исключения — `IdentityHub(webhook_dev_hosts=["localhost"])` для разработки),
  без приватных адресов и редиректов.
- Защита от SSRF. IP-литерал проверяется в любой записи (`2130706433`, `0x7f000001`, `127.1`);
  имена без точки и с суффиксами `.localhost`, `.local`, `.internal` отклоняются сразу. Остальные
  имена резолвятся перед каждым соединением: если хотя бы один адрес приватный или служебный,
  доставка завершается ошибкой `ForbiddenTarget`. Соединение идёт с уже проверенным адресом
  (SNI и `Host` — исходное имя), поэтому подмена DNS между проверкой и соединением не работает.
- Переменные `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` не используются. Исходящий прокси задаётся
  явно: `IdentityHub(webhooks=WebhookPolicy(proxy="http://egress:3128"))`. С прокси имя
  получателя резолвит он, и закрепление адреса не действует — ограничивайте назначения на прокси.
- Путь и query URL считаются секретом получателя: `WebhookEndpoint.url` и read-модели отдают
  `https://host/***`, в логах `httpx` URL доставки замаскирован так же. Полный URL хранится в БД
  открытым текстом.
- Успех — ответ 2xx. Повторы с экспоненциальной задержкой до 24 часов; после 10 неудач подряд
  endpoint отключается с событием `webhook.endpoint.disabled`.
- Доставка идёт параллельно по endpoint'ам (`WebhookPolicy.concurrency`, по умолчанию 8), на
  один endpoint — один запрос за раз, в порядке событий. Медленный получатель занимает один
  слот и не задерживает остальных; после сетевой ошибки или таймаута остальные его доставки
  из текущей пачки откладываются, поэтому недоступный получатель стоит кругу диспетчера не
  больше одного `timeout` (10 с).
- Запрос должен успеть до истечения захвата доставки: `timeout` меньше `claim_for` (30 с), иначе
  `WebhookPolicy` бросает `ValidationFailed`. Доставки, до которых очередь не дошла, возвращаются
  без учёта попытки — второй экземпляр диспетчера не отправит то же событие повторно.
- Результат доставки, начатой до ручного отключения endpoint'а, не включает его обратно и не
  стирает причину отключения.
- Пока endpoint отключён (вручную или после серии отказов), его неотправленные доставки стоят в
  статусе `paused` и в очередь диспетчера не попадают. `set_enabled(endpoint_id, True)` и
  `retry(delivery_id)` возвращают их в `pending`. Статусы доставки: `pending`, `failed` (будет
  повтор), `paused`, `succeeded`, `dead` (попытки исчерпаны).
- Нужен extra `webhooks`.

**Проверка подписи на стороне получателя:**

```python
from identity_hub.dispatch import verify_signature

@app.post("/hooks/identity")
async def hook(request):
    body = await request.body()                        # сырое тело, не перекодированный JSON
    if not verify_signature(body, request.headers["X-Identity-Hub-Signature"], "whsec_..."):
        return Response(status_code=401)
    event = json.loads(body)
    if await seen(event["event_id"]):                  # at-least-once
        return Response(status_code=200)
    ...
```

Формат заголовка: `t=<unix>,v1=<hex>`, подпись — `HMAC-SHA256(secret, f"{t}." + body)`. Во
время ротации приходят две подписи `v1`. Подпись старше 5 минут отвергается; текущее время
можно задать параметром `now=` (`datetime` или Unix-время) — это нужно в тестах с `FakeClock`.

## Telegram-уведомления

```python
channel = await space.notifications.create_telegram_channel(
    "Ops", bot_token=SecretStr("123456:ABC..."), chat_id=-1001234567890, thread_id=None,
)
await space.notifications.create_rule(
    name="Баны",
    channel_id=channel.id,
    event_types=["identity.standing.changed"],
    filter={"service": "telegram", "data.to": ["banned", "frozen"]},
    min_severity=Severity.WARNING,          # по умолчанию WARNING
    digest_seconds=300,                     # копить 5 минут и прислать одной сводкой; None — сразу
    template="{severity} {type}: {link}",   # поля: type, severity, service, actor, link, identity_id, occurred_at
    created_by="admin:ivan",
)
await space.notifications.send_test(channel.id, "проверка")
```

`link` ведёт на карточку аккаунта в админке, если задан `IdentityHub(admin_base_url=...)`.
Нужен extra `telegram`. Ответы Bot API 400/401/403/404 (бот заблокирован, чат не найден)
переводят уведомление в `dead` без повторов.

- **Лимиты Bot API.** Ответ 429 не считается неудачной попыткой: канал ставится на паузу до
  `retry_after`, остальные уведомления этого канала ждут и уходят после паузы. Массовое событие
  (сотни банов сразу) доставляется полностью, со скоростью, которую разрешает Telegram; для
  таких событий удобнее дайджест.
- **Длина сообщения.** Дайджест режется на сообщения до 4096 символов; первое начинается со
  строки «Сводка Identity Hub (N):». Одиночное сообщение длиннее лимита обрезается с «…».
  Если лимит наступил посреди дайджеста, уже отправленные части после паузы не повторяются
  (в пределах жизни процесса диспетчера).
- **Без дублей.** Для правила без дайджеста одно событие даёт одно уведомление, даже если
  диспетчер обработал пачку событий повторно.
- **Шаблон.** Поля — только из списка выше; вложенные поля в формате (`{type:{data}}`) и
  обращения к атрибутам отклоняются при создании правила.
- **Тема чата.** `update_telegram_channel(channel_id, thread_id=None)` сбрасывает тему;
  без аргумента `thread_id` тема не меняется.

Токен бота — часть URL Bot API, а `httpx` пишет URL каждого запроса в лог на уровне `INFO`.
Библиотека вешает фильтр на логгеры `httpx` и `httpcore`: в записях её запросов токен заменён на
`***` (`POST https://api.telegram.org/bot***/sendMessage`). Переменные окружения с прокси
встроенный транспорт не читает; свой `httpx.AsyncClient` можно передать в `HttpTelegramTransport`.

## Журнал

```python
from identity_hub.read import EventFilters

page = await space.admin.event_page(filters=EventFilters(identity_id=identity_id), limit=50)
page = await space.admin.event_page(filters=EventFilters(type="identity.standing.changed", severity="critical"))
```

Дальше: [тестирование](11-testing.md).
