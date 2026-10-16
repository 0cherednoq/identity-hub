# 12. Рецепты

Типовые сценарии целиком. Каждый показывает, какие механизмы библиотеки сочетать.

## Браузерный аккаунт

**Задача:** хранить cookie и localStorage, закрепить отпечаток и прокси, а обновлённый
`storage_state` сохранять автоматически.

```python
from pydantic import BaseModel
from identity_hub import (
    BrowserFingerprint, BrowserSession, Cookies, IdentityCreate,
    PasswordCredentials, ProxyPolicy, Service,
)

class ShopProfile(BaseModel):
    fingerprint: BrowserFingerprint

SHOP = Service(
    name="shop",
    title="Shop",
    credentials=PasswordCredentials,
    session=BrowserSession,
    session_auth_cookies=frozenset({"sessionid"}),
    profile=ShopProfile,
    filterable_profile=frozenset({"fingerprint.browser"}),
    proxy_policy=ProxyPolicy(group="residential", sticky=True),
)

# uploaded может быть JSON, storage_state, Netscape/cookies.txt или их base64.
cookies = Cookies.load(uploaded)
identity = await space.identities.create(
    IdentityCreate(
        service=SHOP,
        external_ref="seller-42",
        credentials={"login": "alice", "password": "secret"},
        session=BrowserSession(cookies=cookies),
        profile={"fingerprint": BrowserFingerprint.load.browserforge(fingerprint_json)},
        lifecycle="ready",
    ),
    actor="intake",
)

pool = space.pools.define("chrome-shops", service=SHOP, holder="worker-1")
async with pool.lease(profile={"fingerprint.browser": "chrome"}) as lease:
    async with lease.playwright_context(browser) as context:
        page = await context.new_page()
        await page.goto("https://shop.example/account")
```

`playwright_context` передаёт в новый context сессию, отпечаток и `lease.proxy`, а при выходе
вызывает `storage_state()` и `lease.save_session()`. Срок записи берётся из самой ранней cookie
`sessionid`; переданный вручную `expires_at` имеет приоритет. Тот же аккаунт можно использовать
без браузера: `cookies=(await lease.session()).cookies.dump.cookiejar()` для httpx/requests и
`proxy=lease.proxy.dump.httpx()`.

## Парсинг каналов пулом аккаунтов

**Задача:** 200 аккаунтов читают публичные каналы, каждый не больше 300 запросов в час.

```python
TELEGRAM = Service(
    name="telegram", title="Telegram",
    credentials=TgCredentials, session=TgSession,
    operations={"read_channel": OperationRule(quotas=(Quota(300, timedelta(hours=1)),))},
    error_classifier=TgErrors(),                 # FLOOD_WAIT → cooling, бан → banned
    proxy_policy=ProxyPolicy(group="dc-proxies", sticky=False),   # IP не важен
)

parsers = space.pools.define("parsers", service=TELEGRAM, tags=["parser"])

async def parse(channel: str):
    async with parsers.lease(for_operation="read_channel", wait=60) as lease:
        client = make_client(await lease.session(), proxy=lease.proxy.reveal() if lease.proxy else None)
        async with lease.operation("read_channel", key=f"read:{channel}:{hour_bucket()}"):
            return await client.read_history(channel)
```

Несколько воркеров на разных машинах запускают `parse` параллельно — аренда гарантирует, что
один аккаунт не используется дважды, а выбор «самого давно не использовавшегося» распределяет
нагрузку по парку.

## Рассылка с имитацией живого поведения

**Задача:** аккаунты шлют сообщения, не больше 20 в час и 100 в сутки, с перерывами, всегда с
одного IP.

```python
TELEGRAM = Service(
    ...,
    operations={"send_message": OperationRule(
        standing=frozenset({Standing.GOOD}),                   # limited-аккаунты не шлют
        quotas=(Quota(20, timedelta(hours=1)), Quota(100, timedelta(days=1))),
    )},
    proxy_policy=ProxyPolicy(group="residential", sticky=True, on_proxy_down=ProxyDownPolicy.WAIT),
    error_classifier=TgErrors(),
)
senders = space.pools.define(
    "senders", service=TELEGRAM, tags=["sender"],
    pacing=Pacing(work=Quota(8, timedelta(hours=1)), pause=(15, 40)),
)
```

Классификатор переводит `PEER_FLOOD` в `limited` — аккаунт перестаёт получать рассылку, но
продолжает использоваться в пулах, где `limited` допустим (например, парсинг).

## Онбординг купленных аккаунтов

**Задача:** поставщик прислал 1000 номеров с паролями; их нужно залогинить, при необходимости
ввести SMS-код и только потом пустить в работу.

```python
from identity_hub import AcceptPolicy, ImportRecord, PasswordCredentials

TELEGRAM = Service(
    ...,
    credentials=PasswordCredentials,
    default_accept=AcceptPolicy(
        activate_on_login=True,
        retire_on=frozenset({"credentials_rejected", "identity_banned", "banned"}),
        max_attempts=3,
    ),
)

def email_password_lines(path):
    for line in path.read_text().splitlines():
        email, password = line.split(maxsplit=1)
        yield ImportRecord(email, credentials={"username": email, "password": password})

report = await space.imports.run(
    service=TELEGRAM,
    records=email_password_lines(path),
    actor="operator",
)
# В отдельном broker/worker-процессе; maintenance вызывает тот же worker автоматически.
processed = await hub.workers.acceptance.run_once(limit=20)
```

Политика хранится вместе с durable-отметкой партии. Успешный вход переводит аккаунт в `ready`,
заданные окончательные коды — в `retired`, а временный отказ остаётся в `provisioning` до
следующего запуска и не превышает `max_attempts`. `retryable=False` блокирует автоматические
повторы и в worker, и в `auth.login_many`; заменить credentials и разрешить новую проверку можно
через `secrets.replace_credentials(..., reset_auth=True)`.

Импорт сессий при непустой политике по умолчанию использует `verify_sessions=True`, поэтому
наличие сессии не обходит проверку. Явный `verify_sessions=False` оставляет прежнее поведение и
выдаёт предупреждение: уже `authenticated` аккаунт будет принят без нового логина. Без
`accept=` и без `Service.default_accept` импорт, как раньше, оставляет аккаунт приложению.

### Несколько паролей-кандидатов на один аккаунт

Первый файл создаёт аккаунт, следующие поставки накапливают кандидатов через `merge`. Имя пары
задавайте, если источник его знает; при конфликте импорт выберет безопасное `import-…` сам.

```python
from identity_hub import CredentialInput, ImportRecord

created = await space.imports.run(
    service=TELEGRAM,
    records=[ImportRecord("user-1", credentials={"username": "user-1", "password": "old"})],
    actor="supplier-a",
)
await space.imports.run(
    service=TELEGRAM,
    records=[
        ImportRecord(
            "user-1",
            credential_records=[
                CredentialInput(
                    "default",
                    {"username": "user-1", "password": "candidate-2"},
                    name="supplier-b",
                )
            ],
        )
    ],
    on_existing="merge",
    actor="supplier-b",
)
await hub.workers.acceptance.run_once()
```

Адаптер перебирает записи через `ctx.credential(...)`, вызывает `reject_credential` для
неподошедших и `verify_credential` для победившей. Отметки применяются вместе с итогом входа:
worker повторит попытку, пока остаётся активная неотклонённая пара, и перестанет брать аккаунт,
когда отвергнуты все кандидаты. `Service(disable_rejected_credentials=True)` дополнительно
переводит проигравшие записи в `disabled`.

Оператор получает Telegram-уведомление о `identity.challenge.opened`, вводит код в вашем UI,
тот вызывает `space.auth.answer_challenge(challenge_id, code, actor="operator")`. Подписчик на
`identity.login.succeeded` переводит аккаунт в `ready`.

## Фоновая синхронизация статусов

**Задача:** узнавать о банах, даже если аккаунт давно не использовался.

```python
TELEGRAM = Service(..., probes=TgProbes(), standing_codes={"USER_DEACTIVATED": "banned"})

async def reconcile_loop(space):
    while True:
        report = await space.sync.refresh_many(regions=("auth", "standing"), not_synced_for=timedelta(hours=6), limit=100)
        await asyncio.sleep(300)
```

Плюс правило уведомлений на `identity.standing.changed` с `data.to: ["banned", "frozen"]`.

## Биржа с KYC

**Задача:** торговать могут аккаунты с подтверждёнными email и телефоном, выводить — только с
полным KYC и не чаще 3 раз в сутки.

Объявление факторов и уровней — в разделе [верификация](06-verification.md). Работа:

```python
traders = space.pools.define("traders", service=EXCHANGE)
async with traders.lease(for_operation="withdraw") as lease:     # выдаст только аккаунт с уровнем full
    async with lease.operation("withdraw", key=f"withdraw:{request.id}") as op:
        if not op.duplicate:
            await exchange(lease).withdraw(request.amount)
        elif op.previous_outcome is None:
            raise NeedsReconciliation(request.id)   # исход прошлой попытки неизвестен — сверить с биржей
```

Повтор заявки после упавшей попытки выполнит вывод заново (`duplicate=False`), после успешной —
пропустит его. Третий случай — [исход неизвестен](08-limits.md#идемпотентность-операции): для
необратимых действий сверяйтесь с платформой, прежде чем повторять.

Когда KYC истекает (`valid_for`), фактор становится `expired`, уровень `full` теряется
(событие `identity.tier.lost`), и аккаунт перестаёт выдаваться под `withdraw`.

## Связка аккаунтов: Telegram + почта для восстановления

```python
mail = await space.identities.create(
    IdentityCreate(service=MAIL, external_ref="box@example.com", credentials=..., lifecycle=Lifecycle.READY)
, actor="operator")
tg = await space.identities.create(
    IdentityCreate(service=TELEGRAM, external_ref="+1...", links={"recovery_email": mail.id})
, actor="operator")

class TgAuthenticator:
    async def login(self, ctx: LoginContext):
        mailbox = await ctx.linked("recovery_email")         # почта взята в аренду на время входа
        code = await read_code_from_mail(await mailbox.credentials())
        ...
```

`await space.links.impact(mail.id)` покажет, какие аккаунты пострадают, если почту
потеряют.

## Кампания создания связанных аккаунтов

Воркеры могут выбирать исходные аккаунты, на которые ещё не ссылается нужный сервис:

```python
pool = space.pools.define("privy-registration", service="twitter", holder=worker_id)
async with pool.lease(not_target_of=("registered_from", "privy")) as lease:
    privy = await space.identities.create(
        IdentityCreate(
            service="privy",
            external_ref=result.id,
            links={"registered_from": lease.identity.id},
        ),
        actor=worker_id,
    )
```

Для собственного планировщика тот же набор доступен через
`identities.list(IdentityFilter(...))`; передайте выбранный id в `lease(among=[id])` и
повторите проверку перед внешним эффектом. Две эксклюзивные аренды не выдадут один исходный
аккаунт одновременно.

## eazy_sdk на хранилище хаба

Схема слота хранит значение SDK и снимок cookies вместе:

```python
from identity_hub import Service
from identity_hub.integrations.eazy import EazySession, HubSessionStore
from eazy_sdk.auth.session import session_lifecycle

EAZY_SERVICE: Service[Credentials, EazySession[SdkSession]] = Service(
    "site", "Site",
    credentials=Credentials,
    session=EazySession[SdkSession],
    authenticator=login_with_eazy,  # входом по-прежнему управляет hub.auth
)

async with space.pools.define("site-runtime", service=EAZY_SERVICE).lease() as lease:
    store: HubSessionStore[SdkSession] = HubSessionStore(lease)
    lifecycle = session_lifecycle(
        SdkSession,
        context_factory=make_context,
        store=store,
        # eazy создаст тот же SessionKey, что store.key("default")
        identity=f"{lease.identity_id}:default",
        name="site session",
    )
    session = await lifecycle.resolve()
    await sdk_call(session.value, proxy=lease.proxy)
```

`HubSessionStore` не берёт и не освобождает аренду: её жизненный цикл остаётся у внешнего
`async with`. `load/save/invalidate` читают и меняют зашифрованный слот под fencing этой аренды;
ревизия eazy сверяется с `await lease.session_revision(slot)`. Вход выполняет объявленный у
сервиса `authenticator`, внутри которого можно использовать eazy_sdk; store не создаёт второе
durable-хранилище. HTTP-клиент ключуйте по прокси текущей аренды, а не только по аккаунту.

## Несколько клиентов в одной базе

```python
acme = await hub.spaces.ensure("acme", "ACME Corp")
globex = await hub.spaces.ensure("globex", "Globex")
```

Аккаунты, прокси, вебхуки и события изолированы по пространствам. Сервисы общие. Один и тот же
аккаунт платформы (`service + external_ref`) не может быть в двух пространствах.

## Работа в транзакции приложения

Регистрация аккаунта вместе с записью в ваших таблицах — атомарно (только PostgreSQL):

```python
async with sessions() as db, db.begin():
    db.add(Order(...))
    await space.using(db).identities.create(IdentityCreate(service=TELEGRAM, external_ref=phone, ...), actor="operator")
# откат транзакции откатит и аккаунт с событием
```
