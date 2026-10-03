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
| `credentials` | пустая модель | Схема данных для входа: пароль, телефон, API-ключи. **Всегда шифруется.** |
| `session` | пустая модель | Схема сессии одного слота: токен, cookie, auth_key. **Всегда шифруется.** |
| `profile` | пустая модель | Персона аккаунта: имя, страна, пол, возраст. Хранится открыто (или частично шифруется). |
| `attributes` | пустая модель | Технические параметры: device_id, user-agent, версия приложения. Открыто. |
| `filterable_profile` | `∅` | Поля профиля, по которым можно фильтровать пулы и поиск (`country:DE`) |
| `searchable_profile` | `∅` | Поля профиля, попадающие в полнотекстовый поиск |
| `profile_encrypted` | `False` | Шифровать профиль; открытыми остаются только `filterable_profile` |
| `operations` | `{}` | Виды операций и их правила допуска и квоты → [лимиты](08-limits.md) |
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

Пустая модель запрещает лишние поля: если сервис не объявил `session`, передать сессию нельзя.

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
from identity_hub import Blame, Fault, LoginChallenge, ProbeResult


class TgAuthenticator:
    async def login(self, ctx):             # вход; вернуть сессию или бросить LoginChallenge
        ...


class TgProbes:
    async def auth(self, ctx):               # жива ли сессия
        ...
    async def standing(self, ctx):           # не забанен ли аккаунт
        ...
    async def verification(self, ctx, factor):
        return None                          # None — «не проверяется»


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
  приходит вид операции;
- `importers` — объекты с `can_import(path)` и async-генератором `read(path)`.

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
