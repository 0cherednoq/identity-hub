# 3. Аккаунты

Identity, или аккаунт, — учётная запись во внешнем сервисе. Все операции — в неймспейсе
`space.identities` (и соседних `space.imports`, `space.secrets`, `space.links`, `space.data`).
Пространство `space = hub.space("production")` создаётся лениво при первом обращении к БД.

Чтение возвращает лёгкий DTO `Identity` без секретов: `id`, `service`, `external_ref`,
регионы (`lifecycle`, `standing`, `auth`, `availability`), `verified_factors`, `tags`,
`labels`, `profile` (открытая часть), `attributes`, `note`, `revision`, `created_at`,
`updated_at`.

## Получить

```python
identity = await space.identities.get(identity_id)                         # или NotFound
identity = await space.identities.find(identity_id)                        # или None
identity = await space.identities.get_by_ref("telegram", "+12025550123")   # по идентификатору платформы
identity = await space.identities.find(service="telegram", external_ref="+12025550123")

card = await space.identities.card(identity_id)
card.regions, card.verifications, card.lease, card.proxy, card.quotas, card.open_challenge
card.eligible, card.blockers        # можно ли сейчас работать и что мешает
```

`get` — для кода, который работает с полями identity; `card` — тяжёлая карточка со
состоянием, квотами, арендой и связями.

### Список и поиск

```python
from identity_hub import IdentityFilter

page = await space.identities.list(
    IdentityFilter(service="telegram", standing=["banned", "frozen"], tags=["parser"]),
    limit=100,
    count=True,
)
page.items, page.next_cursor, page.total
page = await space.identities.list(IdentityFilter(service="telegram"), after=page.next_cursor)

total = await space.identities.count(IdentityFilter(lifecycle="ready"))
page = await space.identities.search("service:telegram standing:banned farm:eu-1")
```

`list` принимает структурированный фильтр (внутри поля со списком — ИЛИ, между полями — И),
`search` — строку запроса:

| Выражение | Что ищет |
|---|---|
| `service:telegram` | сервис |
| `lifecycle:ready`, `standing:banned`, `auth:session_invalid`, `availability:cooling` | значение региона; повтор ключа = ИЛИ |
| `status:active` | `ready` и standing `good`/`limited` |
| `tag:parser` | тег; несколько `tag:` — все сразу |
| `farm:eu-1` | метка или поле `filterable_profile` |
| UUID | точный id |
| слово от 3 символов | подстрока в `external_ref`, заметке, метках, `searchable_profile` |

## Создать

```python
from identity_hub import IdentityCreate, Lifecycle

spec = IdentityCreate(
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
)
identity = await space.identities.create(spec, actor="importer")        # уже есть → AlreadyExists
identity, created = await space.identities.ensure(spec, actor="importer")  # get-or-create
```

Что важно знать:

- **Уникальность.** Identity уникален по `(service, normalized_ref)` во всём хабе, а не только в
  пространстве: тот же номер в другом пространстве даёт `AlreadyExists`. `normalized_ref` — результат
  `Service.normalize_ref(external_ref)`; `external_ref` хранится как ввели. Подробнее — «Нормализация
  external_ref» ниже.
- **`ensure` ничего не обновляет.** Если identity уже есть, он возвращается как есть с
  `created=False`. Для изменений — раздел «Изменить».
- **По умолчанию identity создаётся в `provisioning` и в аренду не выдаётся.** Новый аккаунт
  сначала логинят и проверяют, потом вводят в работу `activate`. Если он готов сразу,
  передайте `lifecycle=Lifecycle.READY`.
- **`auth`** становится `authenticated`, если передана сессия, иначе `no_session`; явно —
  `auth_state=`.
- Данные проверяются схемами сервиса и шифруются **до** обращения к базе. Секреты не попадают
  в `repr(spec)`.
- Несколько сессий: `sessions={"default": {...}, "web": {...}}`. Связи при создании:
  `links={"recovery_email": mail_id}`.

### Путь нового аккаунта

```mermaid
flowchart LR
    A["identities.create()<br/>provisioning"] --> B{"есть сессия?"}
    B -- нет --> C["auth.login()<br/>→ authenticated"]
    B -- да --> D
    C --> D["sync.refresh()<br/>(после activate)"]
    D --> E["identities.activate()"]
    E --> F["в работе:<br/>pool.lease()"]
```

```python
from identity_hub import LoginOutcome

identity = await space.identities.create(IdentityCreate(service=TELEGRAM, external_ref=phone, credentials=creds))
outcome = await space.auth.login(identity.id)            # LoginOutcome: SUCCEEDED / CHALLENGED / FAILED
if outcome is LoginOutcome.SUCCEEDED:
    await space.identities.activate(identity.id, actor="onboarding")
```

Проверить аккаунт пробой (`sync.refresh`) до `activate` в 1.0.0a1 нельзя — см.
[ограничения](limitations.md).


### Нормализация external_ref

Сервис задаёт, какие записи `external_ref` считаются одним аккаунтом. По умолчанию это
`str.strip` (версия `0`). Свою функцию объявляют вместе с версией `≥ 1`:

```python
def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not 10 <= len(digits) <= 15:
        raise ValueError("invalid phone")
    return "+" + digits


TELEGRAM = Service(name="telegram", title="Telegram", normalize_ref=normalize_phone, normalize_ref_version=1)
```

Нормализация выполняется до поиска и вставки: `+380 (50) 123-45-67` и `380501234567` — один
identity, `identities.find(service=..., external_ref=...)` находит его по любой записи, импорт
считает дубли после нормализации и разрешает ссылки `links` так же. Если функция бросает
`ValueError`, запись отклоняется `ValidationFailed` без исходного значения в сообщении.

**Смена нормализатора** — это миграция данных. Увеличьте `normalize_ref_version`, выкатите код
и выполните `await hub.doctor.renormalize(TELEGRAM)`. Метод пересчитывает `normalized_ref`
пачками. Пачку, в которой появились бы дубли, он не применяет и возвращает их id в
`report.collisions`; записи, отвергнутые новой функцией, попадают в `report.invalid`. Разрешите
дубли (удалите или объедините identity) и запустите снова — уже пересчитанные строки не трогаются.

## Создать массово

```python
result = await space.identities.create_many(specs, on_conflict="skip", actor="importer")
result.created      # list[Identity] в порядке (service, external_ref)
result.skipped      # уже существовавшие (только при on_conflict="skip")
```

Одна транзакция, до 1000 записей: вся пачка проверяется до записи, ошибки собираются в один
`ValidationFailed`. `on_conflict="fail"` (по умолчанию) — при любом существующем identity
ничего не создаётся и бросается `AlreadyExists` со списком ref.

`create_many` — для своих данных, которые должны появиться целиком. Для внешних файлов
поставщика с частичным успехом — импорт.

## Импорт

```python
from identity_hub import ImportRecord, OnExisting

records = [
    ImportRecord(
        external_ref="+12025550123",
        credentials={"phone": "+12025550123", "api_id": 1, "api_hash": "..."},
        sessions={"default": {"auth_key": "..."}},
        tags=frozenset({"parser"}),
        labels={"batch": 17},
        proxy="socks5://user:pass@1.2.3.4:1080",   # добавится в группу прокси сервиса
        links={"recovery_email": "mail:box@example.com"},
        verifications={"phone": "verified"},
    ),
    ...
]
report = await space.imports.run(
    service=TELEGRAM,
    records=records,                 # список, генератор или async-генератор
    batch_size=500,
    on_existing=OnExisting.SKIP,     # SKIP | UPDATE_SESSION | FAIL
)
report.created, report.updated, report.skipped, report.failed   # списки id / (ref, причина)
```

| | `identities.create_many` | `imports.run` |
|---|---|---|
| Транзакция | одна на всё | одна на пачку |
| Ошибка записи | откат всего | запись в `failed`, пачка идёт дальше |
| Lifecycle | как в `IdentityCreate` | всегда `provisioning` |
| Существующие | `fail` или `skip` | `skip`, `update_session` или `fail` |
| Связи | по id | по строковому ref, в том числе внутри пачки |
| Прокси и верификации | нет | да |

- `UPDATE_SESSION` обновляет сессии у уже существующих аккаунтов — когда поставщик прислал
  свежие.
- `space.imports.stage(...)` — фоновый импорт до 10 000 записей: данные сохраняются
  зашифрованными, обрабатывает их диспетчер. Состояние — `imports.get(run_id)`, отмена —
  `imports.cancel(run_id)`. Если запись не удалось расшифровать (мастер-ключ убран до ротации),
  прогон завершается `failed` с кодом `staged_payload_unreadable`, а запись остаётся в БД.
- `space.imports.run_files(service=..., paths=[...])` — читает файлы вашими `FileImporter`
  из `Service.importers` (`can_import(path)`, `read(path)` → `ImportRecord`). Готовых
  импортёров `.session`/tdata в библиотеке нет.

## Изменить

| Что | Метод |
|---|---|
| Профиль, метки, заметка — частично, одним событием | `identities.update(id, profile={...}, labels={...}, note="...")` — слияние, `None` в значении удаляет ключ |
| Профиль целиком | `identities.replace_profile(id, {...})` |
| Метки целиком | `identities.replace_labels(id, {...})` |
| Атрибуты целиком | `identities.replace_attributes(id, {...})` |
| Теги | `identities.add_tags(id, ["vip"])`, `remove_tags(id, [...])`, `set_tags(id, [...])` |
| Заметка | `identities.set_note(id, "text")` |
| Credentials | `secrets.replace_credentials(id, {...}, actor="admin")` |
| Сессия вне аренды | `secrets.replace_session(id, {...}, actor="admin", slot="default")` |
| Сессия в аренде | `await lease.save_session(session)` |

```python
identity = await space.identities.update(
    identity_id, profile={"city": "Berlin"}, labels={"farm": "eu-2", "old": None}, actor="ops"
)
```

Метки — плоский словарь: ключ `[A-Za-z0-9_.-]{1,64}`, значение `str/int/float/bool`. Теги —
`[A-Za-z0-9_.:-]{1,64}`. Каждое реальное изменение пишет событие `identity.updated`.
Сменить `external_ref` нельзя.

**Теги или метки?** Теги — для отбора в пулы (`pools.define(..., tags=["parser"])` требует все
теги). Метки — для атрибутов со значением: поставщик, партия, ферма
(`pools.define(..., labels={"farm": "eu-1"})`).

## Состояние

```python
await space.identities.activate(identity_id, actor="onboarding")                 # → ready
await space.identities.retire(identity_id, actor="ops", reason="продан")          # → retired
await space.identities.activate(identity_id, actor="ops", reason="вернули")       # retired → ready, причина обязательна
await space.identities.restore_standing(identity_id, actor="admin", reason="ложный бан")
await space.identities.override(identity_id, Override("standing", "", "banned", reason="жалоба", actor="admin"))
```

Все регионы, машины состояний, бан и синхронизация с платформой — в разделе
[Состояния](04-states.md).

## Удалить

```python
await space.identities.delete(identity_id, actor="ops")
```

`delete` удаляет identity физически; журнал событий сохраняется. Если на identity ссылаются
связи, сначала удалите их (`links.remove`). Для обратимого вывода из работы используйте
`retire`.

Identity с живой арендой не удаляется — `ValidationFailed`: воркер продолжал бы работать с
аккаунтом, которого уже нет. Дождитесь освобождения аренды либо передайте `revoke=True`:
аренда отзывается в той же транзакции (событие `lease.revoked`, история закрывается), воркер
получит `LeaseLost` на ближайшем heartbeat или обращении к секретам.

```python
await space.identities.delete(identity_id, actor="ops", revoke=True)
```

## Секреты

| Данные | Хранение |
|---|---|
| credentials, сессии, evidence верификаций, логины прокси | всегда зашифрованы (AES-256-GCM, свой ключ на запись) |
| profile | открыто; при `profile_encrypted=True` открыты только `filterable_profile` |
| attributes, labels, tags, note | открыто |

```python
# 1. Под арендой — основной путь для воркеров
async with pool.lease() as lease:
    creds = await lease.credentials()
    session = await lease.session()          # None, если сессии нет или она невалидна

# 2. В Authenticator / Probes — через ctx.credentials(), ctx.session()

# 3. Вне аренды — метаданные без значений и раскрытие с аудитом
card = await space.secrets.describe(identity_id)        # есть ли credentials, какие слоты сессий
from identity_hub import SecretRef
data = await space.secrets.reveal(identity_id, SecretRef.CREDENTIALS, actor="admin:ivan", reason="миграция")
data = await space.secrets.reveal(identity_id, SecretRef.session("web"), actor="admin:ivan", reason="миграция")
```

Каждое раскрытие пишет событие `identity.secret.revealed` в той же транзакции. `actor` и `reason`
обязательны и непусты: без автора и причины секрет не раскрывается.

Объект аренды отдаёт секреты, только пока аренда действует. После `release()`, выхода из
`async with`, потери или отзыва аренды, а также когда аккаунт уже взял другой воркер,
`lease.credentials()`, `lease.session()`, `lease.save_session()` и `lease.proxy` бросают `LeaseLost`.

## Связи между аккаунтами

Аккаунт может зависеть от другого: Telegram восстанавливается через почту, биржевой аккаунт
привязан к телефону.

```python
await space.links.add(tg_id, "recovery_email", mail_id)      # одна связь на роль
links = await space.links.of(tg_id)                           # outgoing / incoming
impact = await space.links.impact(mail_id)                    # кого затронет потеря почты
await space.links.remove(tg_id, "recovery_email")
```

Внутри `Authenticator.login` связанный аккаунт доступен через `await ctx.linked("recovery_email")`
— библиотека берёт его в аренду на время входа.

## Свои данные домена

Для состояния вашего приложения (прогресс прогрева, курсоры, счётчики) есть версионируемое
хранилище по неймспейсам:

```python
class Warmup(BaseModel):
    day: int = 0

record = await space.data.load(identity_id, "farm.warmup", Warmup)   # None, если нет
version = await space.data.save(
    identity_id, "farm.warmup", Warmup(day=3),
    expected_version=record.version if record else None,              # защита от гонок
)
```

Параллельная запись с устаревшей версией даёт `ConcurrentModification`. Данные открытые — не
храните в них секреты.

Дальше: [состояния аккаунта](04-states.md).
