# 3. Аккаунты

Все методы вызываются на пространстве: `space = hub.space("production")`. Пространство
создаётся лениво при первом обращении к БД.

## Как добавить аккаунт

### Один аккаунт — `register`

```python
from identity_hub import AuthState, Lifecycle

result = await space.register(
    service=TELEGRAM,
    external_ref="+12025550123",                  # идентификатор на платформе, 1–256 символов
    credentials={"phone": "+12025550123", "api_id": 1, "api_hash": "..."},
    session={"auth_key": "..."},                   # слот "default"; можно не передавать
    profile={"country": "US"},
    attributes={"device": "Pixel 7"},
    tags=["parser", "warm"],
    labels={"farm": "eu-1", "batch": 17},
    note="куплен у поставщика X",
    lifecycle=Lifecycle.READY,                     # по умолчанию PROVISIONING
    actor="importer",
)
result.identity_id    # UUID аккаунта
result.created        # False, если такой аккаунт уже был
```

Что важно знать:

- **Аккаунт уникален по `(service, external_ref)` во всём хабе**, а не только в пространстве.
  Тот же номер в другом пространстве даст `AlreadyExists`.
- **`register` идемпотентен и ничего не обновляет.** Повтор с тем же `external_ref` вернёт
  `created=False` и не изменит данные. Для изменений — методы ниже.
- **По умолчанию аккаунт создаётся в `provisioning` и в аренду не выдаётся.** Это сделано
  намеренно: новый аккаунт сначала проверяют и логинят, потом переводят в `ready`. Если
  аккаунт готов сразу, передайте `lifecycle=Lifecycle.READY`.
- **`auth`** становится `authenticated`, если передана сессия, иначе `no_session`. Можно
  задать явно через `auth_state=`.
- Данные проверяются схемами сервиса и шифруются **до** обращения к базе.
- Несколько сессий (например, мобильная и веб): `sessions={"default": {...}, "web": {...}}`.

### Типовой путь нового аккаунта

```mermaid
flowchart LR
    A["register()<br/>provisioning"] --> B{"есть сессия?"}
    B -- нет --> C["run_login()<br/>→ authenticated"]
    B -- да --> D
    C --> D["проверка<br/>(опционально)"]
    D --> E["set_lifecycle(READY)"]
    E --> F["в работе:<br/>pool.lease()"]
```

```python
reg = await space.register(service=TELEGRAM, external_ref=phone, credentials=creds)
outcome = await space.run_login(reg.identity_id)        # "succeeded" / "challenged" / "failed"
if outcome == "succeeded":
    await space.set_lifecycle(reg.identity_id, Lifecycle.READY, actor="onboarding")
```

Проверить аккаунт пробой (`resync`) до перевода в `ready` в 1.0.0a1 нельзя — см.
[ограничения](limitations.md).

### Много аккаунтов — `import_many`

```python
from identity_hub import ImportRecord, OnExisting

records = [
    ImportRecord(
        external_ref="+12025550123",
        credentials={"phone": "+12025550123", "api_id": 1, "api_hash": "..."},
        sessions={"default": {"auth_key": "..."}},
        tags=frozenset({"parser"}),
        labels={"batch": 17},
        proxy="socks5://user:pass@1.2.3.4:1080",   # добавится в пул прокси сервиса
        links={"recovery_email": "mail:box@example.com"},
        verifications={"phone": "verified"},
    ),
    ...
]
report = await space.import_many(
    service=TELEGRAM,
    records=records,                 # список, генератор или async-генератор
    batch_size=500,
    on_existing=OnExisting.SKIP,     # SKIP | UPDATE_SESSION | FAIL
)
report.created, report.updated, report.skipped, report.failed   # списки id / (ref, причина)
```

- Каждая пачка — отдельная транзакция; ошибка одной записи не роняет пачку.
- Импортированные аккаунты всегда в `provisioning`.
- `UPDATE_SESSION` обновляет сессии у уже существующих аккаунтов — удобно, когда поставщик
  прислал свежие сессии.
- `space.stage_import(...)` — фоновый вариант до 10 000 записей: данные сохраняются
  зашифрованными, а обрабатывает их диспетчер. Отмена — `space.cancel_import(run_id)`.
- `space.import_files(service=..., paths=[...])` — читает файлы вашими `FileImporter`
  из `Service.importers` (`can_import(path)`, `read(path)` → `ImportRecord`). Готовых
  импортёров `.session`/tdata в библиотеке нет.

## Как изменить данные

| Что | Метод |
|---|---|
| Метки (ключ-значение) | `set_labels(id, {"farm": "eu-2"}, merge=True)` — `None` удаляет ключ |
| Теги | `add_tags(id, ["vip"])`, `remove_tags(id, [...])`, `set_tags(id, [...])` |
| Профиль | `update_profile(id, {"city": "Berlin"})` — слияние; `replace=True` — замена |
| Атрибуты | `set_attributes(id, {...})` — полная замена |
| Заметка | `set_note(id, "text")` |
| Credentials | `update_credentials(id, {...}, actor="admin")` — полная замена |
| Сессия вне аренды | `update_session(id, {...}, actor="admin", slot="default")` |
| Сессия в аренде | `await lease.save_session(session)` |

Метки — плоский словарь: ключ `[A-Za-z0-9_.-]{1,64}`, значение `str/int/float/bool`. Теги —
`[A-Za-z0-9_.:-]{1,64}`. Каждое реальное изменение пишет событие `identity.updated`.
Сменить `external_ref` нельзя.

**Теги или метки?** Теги — для отбора в пулы (`pool(tags=["parser"])` требует все теги).
Метки — для атрибутов со значением: поставщик, партия, ферма (`pool(labels={"farm": "eu-1"})`).

## Секреты: где лежат и как их получить

| Данные | Хранение |
|---|---|
| credentials, сессии, evidence верификаций, логины прокси | всегда зашифрованы (AES-256-GCM, свой ключ на запись) |
| profile | открыто; при `profile_encrypted=True` открыты только `filterable_profile` |
| attributes, labels, tags, note | открыто |

Получить секрет можно тремя путями:

```python
# 1. Под арендой — основной путь для воркеров
async with pool.lease() as lease:
    creds = await lease.credentials()
    session = await lease.session()          # None, если сессии нет или она невалидна

# 2. В Authenticator / Probes — через ctx.credentials(), ctx.session()

# 3. Вне аренды — только с аудитом (событие identity.secret.revealed)
data = await space.reveal_secret(identity_id, "credentials", actor="admin:ivan", reason="миграция")
# поле: "credentials" | "session" | "session:<slot>" | "profile" | "evidence:<factor>"
```

## Связи между аккаунтами

Аккаунт может зависеть от другого: Telegram восстанавливается через почту, биржевой аккаунт
привязан к телефону.

```python
await space.link(tg_id, "recovery_email", mail_id)      # одна связь на роль
links = await space.links_of(tg_id)                      # outgoing / incoming
impact = await space.impact_of(mail_id)                  # кого затронет потеря почты
await space.unlink(tg_id, "recovery_email")
```

Внутри `Authenticator.login` связанный аккаунт доступен через `await ctx.linked("recovery_email")`
— библиотека берёт его в аренду на время входа.

## Свои данные домена

Для состояния вашего приложения (прогресс прогрева, курсоры, счётчики) есть версионируемое
хранилище по неймспейсам:

```python
class Warmup(BaseModel):
    day: int = 0

record = await space.load_data(identity_id, "farm.warmup", Warmup)   # None, если нет
version = await space.save_data(
    identity_id, "farm.warmup", Warmup(day=3),
    expected_version=record.version if record else None,              # защита от гонок
)
```

Параллельная запись с устаревшей версией даёт `ConcurrentModification`. Данные открытые — не
храните в них секреты.

## Поиск и карточка

```python
page = await space.search("service:telegram standing:banned tag:parser farm:eu-1", limit=100)
for row in page.items: ...
page = await space.search("...", after=page.next_cursor)          # следующая страница

card = await space.identity_card(identity_id)
card.identity.standing, card.regions, card.lease, card.quotas
card.eligible, card.blockers        # можно ли сейчас работать и что мешает
```

Синтаксис запроса:

| Выражение | Что ищет |
|---|---|
| `service:telegram` | сервис |
| `lifecycle:ready`, `standing:banned`, `auth:session_invalid`, `availability:cooling` | значение региона; повтор ключа = ИЛИ |
| `status:active` | `ready` и standing `good`/`limited` |
| `tag:parser` | тег; несколько `tag:` — все сразу |
| `farm:eu-1` | метка или поле `filterable_profile` |
| UUID | точный id |
| слово от 3 символов | подстрока в `external_ref`, заметке, метках, `searchable_profile` |

Отдельного метода «получить аккаунт по id» нет — используйте `identity_card`.

## Вывод из работы и удаление

```python
await space.retire(identity_id, actor="ops", reason="продан")     # lifecycle → retired, данные сохраняются
await space.set_lifecycle(identity_id, Lifecycle.READY, actor="ops", reason="вернули")  # обратно, причина обязательна
await space.delete(identity_id, actor="ops")                        # физическое удаление
```

`retire` — обратимый вывод, аккаунт остаётся в базе и истории. `delete` удаляет аккаунт
(журнал событий сохраняется); если на аккаунт ссылаются связи, сначала удалите их.

Дальше: [состояния аккаунта](04-states.md).
