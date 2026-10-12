# 3. Аккаунты

Identity, или аккаунт, — учётная запись во внешнем сервисе. Все операции — в неймспейсе
`space.identities` (и соседних `space.imports`, `space.secrets`, `space.links`, `space.data`).
Пространство создаётся явно — `space = await hub.spaces.ensure("production", "Production")` — и
дальше берётся по slug: `hub.space("production")`. Если пространства нет, первая операция с
`hub.space(...)` даёт `NotFound`: опечатка в slug не создаёт пустое пространство.

Чтение возвращает лёгкий DTO `Identity` без секретов: `id`, `service`, `external_ref`,
регионы (`lifecycle`, `standing`, `auth`, `availability`), `verified_factors`, `tags`,
`labels`, `profile` (открытая часть), `attributes`, `note`, `revision`, `created_at`,
`updated_at`.

`revision` — число команд, изменивших аккаунт: у нового аккаунта 1, каждая команда изменения
добавляет 1, сколько бы событий она ни записала. Захват аренды, операции и раскрытие секрета
ревизию не меняют, команда без реальных изменений — тоже. Ту же ревизию несут события команды
([События](10-events.md#событие)) и колонка `hub.read.identities.c.revision`.

### Повтор замены секрета: `idempotency_key`

`secrets.replace_credentials` и `secrets.replace_session` принимают `idempotency_key`. Повтор с
тем же ключом и теми же данными ничего не пишет (нет второго события и новой ревизии), с другими
данными — `IdempotencyConflict`. Секрет в ключ не попадает: отпечаток команды строится по его
HMAC-тегу ключом отпечатков (`D-208`). Ключ живёт `Retention.operation_keys`; после ротации ключа
отпечатков повтор сверяется прежним ключом, а если его убрали из конфигурации раньше срока —
`SecretConfigurationError`.

```python
await space.secrets.replace_credentials(
    identity_id, {"phone": "+1...", "password": new_password}, actor="ops", idempotency_key=request_id
)
```

### Параллельные правки: `expected_revision`

Каждая команда изменения аккаунта принимает `expected_revision` — ревизию, которую видел
оператор. Если аккаунт с тех пор изменили, команда ничего не пишет и бросает
`RevisionConflict` (`expected_version` — переданная ревизия, `actual_version` — текущая):

```python
card = await space.identities.get(identity_id)
try:
    await space.identities.update(
        identity_id, labels={"owner": "ops"}, actor="admin", expected_revision=card.revision
    )
except RevisionConflict:
    ...  # перечитать карточку и показать оператору свежее состояние
```

Параметр есть у `identities.update`, `replace_*`, `add_tags`/`remove_tags`/`set_tags`,
`set_note`, `delete`, `activate`, `retire`, `set_lifecycle`, `restore_standing`, `override`,
`unpin`, `secrets.replace_credentials`/`replace_session`, `links.add`/`remove` и
`verifications.requirements.add`/`resolve`, `verifications.review`. Без параметра (`None`) команда
применяется безусловно. Проверка идёт под блокировкой строки аккаунта в транзакции команды: из
двух команд с одной ревизией проходит одна. В `space.using(session)` конфликт откатывает только
savepoint команды, транзакция приложения продолжается.

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
page.items, page.next_cursor, page.total   # total — точно до 1000, больше — 1001; точное — count()
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
| `id:9f3c2a` | окончание id (шестнадцатеричные цифры без дефисов, от 3 символов) |
| слово от 3 символов | подстрока в `external_ref`, заметке, метках, `searchable_profile` |

Строка запроса — не длиннее 1000 символов и не больше 50 условий; иначе `ValidationFailed`.

Страница для админки `space.admin.identity_page(query, options=AccountSearchOptions(sort_by=...))`
сортирует по `created_at`, `external_ref`, `service`, регионам, `error_score`, `last_leased_at`,
`synced_at`, `eligible`, `leased`, `verification_tier` или по полю профиля — `profile:<key>` для
ключа из `filterable_profile`. Строки сравниваются по кодам символов, одинаково на любой базе:
заглавные латинские буквы идут раньше строчных. Поле профиля сортируется как текст, поэтому
числа идут в порядке `"100"`, `"25"`, `"4"`; аккаунты без значения — в конце. Курсор `after`
действителен только для той сортировки, с которой получен: чужой или изменённый курсор —
`ValidationFailed`.

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
  `auth=`.
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

identity = await space.identities.create(IdentityCreate(service=TELEGRAM, external_ref=phone, credentials=creds), actor="operator")
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
, actor="operator")
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

- **Одна негодная запись не роняет импорт.** Запись, которая не прошла проверку (схема
  credentials или сессии, негодный URL прокси, неизвестный фактор), попадает в `report.failed`
  с причиной; остальные записи пачки импортируются. Причина называет поле
  (`credentials.password`), но не содержит значений — ни пароля, ни URL, ни ref связанного
  аккаунта.
- **Связи.** Цель связи ищется в этой же пачке и в базе пространства. Если её нет, отклоняется
  только эта запись (`связь <роль>: связанный аккаунт не найден`).
- **Прокси.** Прокси из `ImportRecord.proxy` создаётся с ёмкостью `max_identities` (параметр
  `imports.run`, по умолчанию 3). Запись, которой на прокси уже не хватает места, отклоняется.
- `UPDATE_SESSION` обновляет сессии у уже существующих аккаунтов — когда поставщик прислал
  свежие. Запись сопоставляется с аккаунтом по нормализованному `external_ref`. С
  `verify_sessions=True` новая сессия не считается рабочей: `auth` аккаунта становится
  `no_session`, пока её не подтвердит вход или проба.
- Пачка, в которой есть созданные или обновлённые аккаунты, порождает событие
  `identity.imported` (`count`, `updated`, `skipped`).
- `space.imports.stage(...)` — фоновый импорт до 10 000 записей: данные сохраняются
  зашифрованными, обрабатывает их диспетчер. Состояние — `imports.get(run_id)`, отмена —
  `imports.cancel(run_id)`. Если запись не удалось расшифровать (мастер-ключ убран до ротации),
  прогон завершается `failed` с кодом `staged_payload_unreadable`, а запись остаётся в БД.
  Прогоны пространств независимы: сбой одного не задерживает остальные. Запись, обработка
  которой падает с непредвиденной ошибкой, повторяется после истечения захвата и на третьей
  попытке помечается `failed` (`staged_item_failed`); прогон при этом доходит до конца.
- `space.imports.run_files(service=..., paths=[...], actor="operator")` — читает файлы вашими `FileImporter`
  из `Service.importers` (`can_import(path)`, `read(path)` → `ImportRecord`: async-генератор или
  список). Готовых импортёров `.session`/tdata в библиотеке нет. Файл без импортёра и файл,
  который импортёр не смог прочитать, попадают в `report.failed` под именем файла; остальные
  файлы импортируются. `base_dir=` — относительные пути считаются от каталога, путь за его
  пределами (через `..` или абсолютный) попадает в `failed`; пути из внешнего ввода (загрузка
  в админке, API) передавайте только так. `max_bytes=` — файл, а для каталога (tdata) суммарный
  размер, больше лимита не читается и попадает в `failed`.

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
| Сессия вне аренды | `secrets.replace_session(id, {...}, actor="admin", slot="default", mode="in_place")`; `mode="replace"` — новая запись, прежняя в истории; `secrets.revoke_session(id, slot, actor=...)` — отзыв без замены |
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

# пачкой, до 1000 аккаунтов одной транзакцией
result = await space.identities.retire_many(ids, actor="ops", reason="продан")
result.applied                                   # id изменённых
[(r.key, r.code) for r in result.rejected]       # отказы: not_found, invalid_transition, …
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

### Несколько credentials

У аккаунта может быть несколько секретов: пароль, TOTP, API-ключи, коды восстановления — каждый
со своим сроком и отзывом (`D-214`, `D-215`). Вид (`kind`) объявляет сервис со своей схемой;
вид `default` — это прежние `credentials` сервиса. Записей одного вида может быть несколько —
их различает имя (`name`).

```python
class ApiKey(BaseModel):
    secret: SecretStr

EXCHANGE = Service("exchange", "Exchange", credentials=Login, credential_kinds={"api_key": ApiKey})

await space.secrets.put_credential(
    identity_id, "api_key", {"secret": key}, name="trading",
    public_value={"key_id": "AK-1"},        # открытая часть: видна в списке и админке
    expires_at=deadline, actor="ops",
)
for item in await space.secrets.credentials(identity_id):   # без секретов
    print(item.kind, item.name, item.lifecycle, item.expired, item.public_value)
await space.secrets.disable_credential(identity_id, "api_key", "trading", actor="ops")
await space.secrets.enable_credential(identity_id, "api_key", "trading", actor="ops")
await space.secrets.revoke_credential(identity_id, "api_key", "trading", actor="ops", reason="утечка")
```

- Жизненный цикл: `active ↔ disabled`, `revoked` — конечное: включить или заменить отозванную
  запись нельзя (`TransitionNotAllowed`), повторный отзыв возвращает `False`.
- Срок считается по времени: `expired` — запись активна, но `expires_at` прошёл; отдельного
  перехода нет.
- Каждая команда пишет событие `identity.credentials.updated` с видом, именем и действием
  (`put`, `disable`, `enable`, `revoke`) — без секрета и без `public_value` — и поднимает ревизию
  аккаунта. Все команды принимают `expected_revision`.

Под арендой и в `Authenticator`/пробах запись берут по виду и имени (`D-216`):

```python
key = await lease.credential("api_key", "trading")       # None: нет, выключена, отозвана, истекла
everything = await lease.active_credentials()             # {("api_key", "trading"): ApiKey(...), ...}
login = await ctx.credential("default")                   # то же, что ctx.credentials()
value = await space.secrets.reveal(
    identity_id, SecretRef.credential("api_key", "trading"), actor="admin", reason="проверка"
)
```

`lease.credentials()` по-прежнему отдаёт запись `default`. Раскрытие пишет
`identity.secret.revealed` с видом и именем записи, но без значения.

Записи можно передать сразу при создании и импорте (`D-217`): они проверяются и шифруются до
транзакции и пишутся вместе с аккаунтом. Импорт пишет их только новому аккаунту — повтор импорта
дубликатов не создаёт. `secrets.describe(id).credential_records` перечисляет все записи без
секретов.

```python
from identity_hub import CredentialInput

await space.identities.create(
    IdentityCreate(
        service=EXCHANGE, external_ref="trader-1", credentials={"login": "t1", "password": pw},
        credential_records=[CredentialInput("api_key", {"secret": key}, name="trading")],
    ),
    actor="ops",
)
ImportRecord(external_ref="trader-2", credential_records=[CredentialInput("api_key", {"secret": key2})])
```

## Связи между аккаунтами

Аккаунт может зависеть от другого: Telegram восстанавливается через почту, биржевой аккаунт
привязан к телефону.

```python
await space.links.add(tg_id, "recovery_email", mail_id, actor="operator")      # одна связь на роль
links = await space.links.of(tg_id)                           # outgoing / incoming
impact = await space.links.impact(mail_id)                    # кого затронет потеря почты
await space.links.remove(tg_id, "recovery_email", actor="operator")
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
