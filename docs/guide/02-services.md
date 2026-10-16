# 2. Сервисы: как описать платформу

`Service` — описание одной платформы в коде. Сервис объявляют один раз и передают в
`IdentityHub(services=[...])`. В базе хранится только имя и заголовок сервиса; схемы, правила
и протоколы берутся из кода при каждом обращении. **Код — источник правды о сервисе.**

## Минимальный сервис

```python
from pydantic import BaseModel, SecretStr
from identity_hub import Service


class TgCredentials(BaseModel):
    phone: str
    api_id: int
    api_hash: SecretStr


class TgSession(BaseModel):
    auth_key: SecretStr
    dc_id: int = 2


TELEGRAM = Service(
    name="telegram",          # ^[a-z][a-z0-9_]{0,63}$, уникально в хабе
    title="Telegram",
    credentials=TgCredentials,
    session=TgSession,
)
```

Ошибки объявления (неизвестный фактор, фильтр по несуществующему полю профиля, цикл
зависимостей) собираются все сразу и бросаются одним `ValidationFailed` при создании `Service`.

## Все поля `Service`

| Поле | По умолчанию | Назначение |
|---|---|---|
| `name`, `title` | обязательны | Имя (идентификатор) и заголовок для админки |
| `credentials` | пустая модель | Схема данных для входа: пароль, телефон, API-ключи. По умолчанию шифруется; режим хранения задаёт хаб. |
| `session` | пустая модель | Схема сессии одного слота: токен, cookie, auth_key. По умолчанию шифруется; режим хранения задаёт хаб. |
| `session_slots` | `{}` | Схемы отдельных слотов (`{"api": TokenSession}`); остальные слоты используют `session`. |
| `session_auth_cookies` | `∅` | Имена cookie, по минимальному сроку которых автоматически задаётся `expires_at` у `BrowserSession`. |
| `profile` | пустая модель | Персона аккаунта: имя, страна, пол, возраст. Хранится открыто (или частично шифруется). |
| `attributes` | пустая модель | Технические параметры: device_id, user-agent, версия приложения. Открыто. |
| `filterable_profile` | `∅` | Поля профиля для фильтра пулов, включая вложенные пути (`country`, `fingerprint.browser`) |
| `searchable_profile` | `∅` | Поля профиля, попадающие в полнотекстовый поиск |
| `profile_encrypted` | `False` | Шифровать профиль; открытыми остаются только `filterable_profile` |
| `operations` | `{}` | Виды операций и их правила допуска и квоты → [лимиты](08-limits.md) |
| `throttles` | `{}` | Именованные GCRA-лимиты частоты запросов → [лимиты](08-limits.md#троттлинг-частоты) |
| `lease_mode` | `EXCLUSIVE` | `SHARED` — аренда не блокирует аккаунт → [аренда](07-leases.md) |
| `proxy_policy` | `None` | Как выдавать прокси аккаунтам → [прокси](09-proxies.md) |
| `authenticator` | `None` | Ваша реализация входа → [вход](05-login.md) |
| `probes` | `None` | Ваши проверки статуса на платформе → [синхронизация](04-states.md#синхронизация-статусов-с-платформой) |
| `error_classifier` | `None` | Перевод исключений платформы в последствия для аккаунта → [лимиты](08-limits.md#классификатор-ошибок) |
| `standing_codes` | `{}` | Коды статусов платформы → `Standing` (`{"USER_DEACTIVATED": "banned"}`) |
| `normalizers` | `{}` | Функции нормализации наблюдений для синхронизируемых регионов |
| `verifications`, `tiers` | `()` | Факторы и уровни верификации → [верификация](06-verification.md) |
| `quarantine_threshold` | `5.0` | Порог `error_score` для автоматического карантина |
| `importers` | `()` | Ваши читатели файлов для `imports.run_files` |
| `default_accept` | `None` | Политика проверки новых импортированных аккаунтов через `hub.workers.acceptance` |
| `disable_rejected_credentials` | `False` | Сразу переводить отмеченную адаптером отклонённую пару credentials в `disabled` |

Пустая модель запрещает лишние поля: если сервис не объявил `session`, передать сессию нельзя.

Формат хранения выбирается на уровне процесса, не сервиса:
`IdentityHub(..., secret_storage="encrypted" | "plaintext")`. Значение по умолчанию —
`plaintext`; смешанная база читается независимо от настройки процесса. Подробности, SQL и
перевод существующих строк — в разделе [Секреты](03-accounts.md#секреты).

Разные виды авторизации одного аккаунта можно типизировать независимо:

```python
service = Service(
    "shop",
    "Shop",
    session=BrowserSession,
    session_slots={"api": TokenSession},
)
```

`default` в этом примере проверяется как `BrowserSession`, `api` — как `TokenSession`; любой
необъявленный слот сохраняет совместимое прежнее поведение и использует общую `session`.

## Стандартные форматы

Модели из `identity_hub.formats` необязательны: сервис может по-прежнему объявить свою
Pydantic-модель. Для браузерного аккаунта обычно достаточно:

```python
class ShopProfile(BaseModel):
    fingerprint: BrowserFingerprint

SHOP = Service(
    name="shop",
    title="Shop",
    credentials=PasswordCredentials,
    session=BrowserSession,
    session_slots={"api": TokenSession},
    session_auth_cookies=frozenset({"sessionid"}),
    profile=ShopProfile,
    filterable_profile=frozenset({"fingerprint.browser"}),
)
```

`session_auth_cookies` применяется при регистрации, импорте, `lease.save_session()` и
`secrets.replace_session()`. Если передан свой `expires_at`, он имеет приоритет. Сессионная
auth-cookie или отсутствие подходящих cookie оставляет срок пустым. Вложенный фильтр задаётся
так: `pool.lease(profile={"fingerprint.browser": "chrome"})`. Для `profile_encrypted=True`
используйте плоское открытое поле: частичное раскрытие вложенной модели не поддерживается.

| Модель и формат | Выгрузка / загрузка | Явные потери |
|---|---|---|
| Канонический JSON | `dump.json()` / `load.json()` | нет |
| Playwright | `Cookies.dump/load.playwright()`, `BrowserSession.dump/load.playwright()` | `sessionStorage`; Chromium может нормализовать незаданный SameSite |
| eazy_sdk | `Cookies.dump/load.eazy()`, `BrowserSession.dump/load.eazy_state()` | `partition_key`; `CookieState` хранит только cookies |
| CDP / Puppeteer | `dump/load.cdp()`, `dump.puppeteer()` | Puppeteer — только выгрузка |
| Selenium | `dump/load.selenium()` | partitioned cookie пропускаются |
| Chrome extensions | `dump/load.chrome_extension()` | служебные поля расширений не канонизируются |
| Crawlee Python / JS | `dump/load.crawlee()`, `dump/load.tough_cookie()` | поля вне cookie-схемы Crawlee |
| stdlib / HTTP | `dump/load.cookiejar()`, `dump.aiohttp()`, `load.set_cookie()`, `dump.scrapy()` | Scrapy теряет срок, HttpOnly и SameSite; partitioned не поддерживаются |
| Netscape / curl | `dump/load.netscape()` | SameSite и partition key |
| HAR | `load.har()` | только загрузка; HAR 1.2 не задаёт SameSite |
| Автоопределение | `Cookies.load(data)`, `BrowserSession.load(data)` | принимает только перечисленные безопасные формы |
| Отпечаток | `BrowserFingerprint.load.browserforge/fingerprint_suite()`, `dump.playwright_context/camoufox/adspower/gologin/impersonate()` | несовпадение UA и TLS-цели, не-Firefox для Camoufox |

Потеря выдаёт `FormatLossWarning`; `strict=True` превращает её в `ValidationFailed`. Значения
cookie, токенов, паролей и URL прокси скрыты в `repr`. Pickle и Chrome `Cookies` SQLite не
загружаются. Конвертеры не требуют установки Playwright, Selenium или HTTP-клиентов.

## Операции и правила допуска

Операция — вид действия на платформе, для которого нужны свои условия: «прочитать канал»,
«отправить сообщение», «вывести средства».

```python
from datetime import timedelta
from identity_hub import OperationRule, Quota, Standing

TELEGRAM = Service(
    name="telegram",
    title="Telegram",
    credentials=TgCredentials,
    session=TgSession,
    operations={
        # чтение разрешено и аккаунтам с ограничениями
        "read_channel": OperationRule(quotas=(Quota(300, timedelta(hours=1)),)),
        # отправка — только полностью здоровым, не больше 20 в час и 100 в сутки
        "send_message": OperationRule(
            standing=frozenset({Standing.GOOD}),
            quotas=(Quota(20, timedelta(hours=1)), Quota(100, timedelta(days=1))),
        ),
    },
)
```

`OperationRule`:

| Поле | По умолчанию | Смысл |
|---|---|---|
| `standing` | `{good, limited}` | Допустимые значения standing |
| `needs_auth` | `True` | Требуется `auth = authenticated` |
| `verified` | `∅` | Обязательные факторы верификации |
| `tiers` | `∅` | Обязательные уровни верификации |
| `quotas` | `()` | Лимиты на аккаунт в фиксированных окнах |

Любая операция дополнительно требует `lifecycle = ready` и доступности (`availability`). В
каждый сервис автоматически добавляется служебная операция `probe`: она не требует
авторизации и допускает любой standing — её используют пробы.

## Протоколы, которые реализует ваш код

Сервис связывает библиотеку с платформой через три необязательных протокола. Все —
обычные классы с нужными методами, наследоваться ни от чего не требуется.

```python
from identity_hub import Blame, Fault, LoginChallenge, LoginContext, ProbeContext, ProbeResult


class TgAuthenticator:
    async def login(self, ctx: LoginContext):    # вход; вернуть сессию или бросить LoginChallenge
        ...


class TgProbes:                                  # любое подмножество auth / standing / verification
    async def auth(self, ctx: ProbeContext):     # жива ли сессия; None — «не знаю»
        ...
    async def standing(self, ctx: ProbeContext): # не забанен ли аккаунт
        ...


class TgErrors:
    def classify(self, exc):                 # исключение платформы → Fault или None
        ...


TELEGRAM = Service(
    name="telegram", title="Telegram",
    credentials=TgCredentials, session=TgSession,
    authenticator=TgAuthenticator(),
    probes=TgProbes(),
    error_classifier=TgErrors(),
    standing_codes={"USER_DEACTIVATED": "banned", "PEER_FLOOD": "limited"},
)
```

Требования к адаптерам проверяются при объявлении `Service` — ошибка сразу, а не на первом входе:

- `authenticator` — объект с `async def login(ctx)` или сама функция `async def login(ctx)`;
  `refresh` не нужен (необязательный протокол `SessionRefresher` библиотека пока не вызывает);
- `probes` — объект с любым подмножеством `async def auth/standing/verification`: регион без пробы
  не сверяется;
- `error_classifier` — объект с `classify(exc)` или словарь `{TimeoutError: Fault(Blame.PROXY)}`
  (подходящий тип ищется по MRO исключения); если `classify` принимает именованный `kind`, туда
  приходит вид операции. Невалидный `Fault` (например, наивный `until` или срок у `frozen`)
  считается ошибкой классификатора: она логируется, а исход обрабатывается как `Blame.REQUEST`;
- `importers` — объекты с `can_import(path)` и `read(path)`: async-генератор с `yield ImportRecord`,
  обычная функция или `async def`, возвращающие список записей.

Что адаптер получает в `ctx`:

| `LoginContext` | `ProbeContext` |
|---|---|
| `identity_id`, `external_ref`, `proxy_url` | `identity_id`, `external_ref`, `proxy_url` |
| `await ctx.credentials()` | `await ctx.credentials()` |
| `await ctx.session(slot)` — прежняя сессия, в том числе недействительная | `await ctx.session(slot)` — действующая сессия |
| `challenge_answer`, `challenge_context`, `await ctx.linked(role)` | — |

Подробно: [вход](05-login.md), [пробы](04-states.md#синхронизация-статусов-с-платформой),
[классификатор ошибок](08-limits.md#классификатор-ошибок).

## Регистрация в хабе и изменение сервиса

```python
hub = IdentityHub(sessionmaker=sessions, secrets=EnvSecretsProvider(), services=[TELEGRAM, MAIL])
await hub.start()   # upsert имён сервисов в БД — вызвать до первой регистрации аккаунта
```

Что происходит при изменении объявления в коде:

- **Схемы** применяются при каждой записи и чтении. Старые данные не мигрируются: если новая
  схема несовместима, `identities.update` бросит `ValidationFailed`, а `lease.session()` —
  ошибку валидации pydantic. Добавляйте новые поля со значениями по умолчанию.
- **`searchable_profile` / `filterable_profile`** — после изменения вызовите
  `await space.maintenance.reindex_search(TELEGRAM)`.
- **Правила операций и квоты** действуют сразу для новых операций.
- **Удалённые факторы верификации** правилами игнорируются.
- **Удалённый из кода сервис** остаётся в БД, но операции с его аккаунтами падают `NotFound`.

Дальше: [аккаунты](03-accounts.md).
