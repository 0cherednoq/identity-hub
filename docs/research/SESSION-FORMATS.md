# Стандартные форматы сессий, cookies, отпечатков и credentials

Исследование от 2026-10-15. Цель — выбрать формат хранения и набор конвертеров, чтобы сессию из
Identity Hub можно было без потерь отдать в браузер (Playwright, Selenium, CDP), в HTTP-клиент
(httpx, requests, curl_cffi, aiohttp) и в антидетект, а также принять её оттуда.

Факты собраны по исходникам и официальной документации. Где проверено запуском кода, указано
отдельно. Непроверенное собрано в разделе 9. Версии инструментов указаны в разделе 10.

Статус: **исследование и предложение**. В план задачи пока не внесены.

---

## 1. Коротко

1. **Единого формата cookie нет.** На практике встречаются четыре семейства, и все они
   различаются регистром ключей, единицами времени и записью `sameSite`:
   - Playwright;
   - CDP (Puppeteer, nodriver, Botasaurus);
   - `chrome.cookies` (Cookie-Editor, EditThisCookie, AdsPower, GoLogin);
   - Netscape `cookies.txt` (curl, `MozillaCookieJar`).

   Отдельно стоят `http.cookiejar` (база для httpx, requests, curl_cffi, tls-client), Selenium
   (W3C), tough-cookie (Crawlee JS) и HAR.
2. **Сессия браузера на практике — это Playwright `storage_state`**: cookies и `localStorage`
   по origin. Его используют Crawlee-Python (через конвертер), Camoufox, browser-use и
   scrapy-playwright.
3. **Внутри храним свою каноническую модель** в snake_case, без потерь, с явными `host_only`,
   `expires=None` для сессионной cookie и `same_site=None` для «не задан». В каждый внешний
   формат переводим конвертером, и потери у каждого формата перечислены явно.
4. **Отпечаток браузера хранится в профиле аккаунта, а не в сессии.** Он должен пережить
   повторный вход, и он не секрет. За основу берём схему fingerprint-suite/browserforge: её
   используют Crawlee и Camoufox, а все антидетекты описывают отпечаток похожими полями.
   Добавляем то, чего в ней нет: `timezone`, `locale`, `geolocation`, `webrtc`.
5. **Креды и сессии классифицируются по форме данных, а не по протоколу** (раздел 4.3).
   Basic, OAuth 2.0, OIDC, CAS и SAML ложатся на несколько моделей:
   - креды: `PasswordCredentials`, `OtpSecret`, `RecoveryCodes`, `ApiKey`, `SigningKey`,
     `OAuthClient`, `OAuth1Client`, `PrivateKeyCredentials`, `ClientCertificate`;
   - сессии: `BrowserSession`, `TokenSession` (OAuth 2.0 и OIDC), `OAuth1Session`,
     `HeaderSession`.
   Почта для восстановления — отдельный связанный аккаунт, а не вид кредов.
6. **API конвертации — неймспейсы `dump` и `load` на моделях:** `session.dump.playwright()`,
   `Cookies.load.netscape(text)`, `Cookies.load(data)` с автоопределением (раздел 5.0). Импорт
   модулей форматов не нужен. Результат — обычные `dict`, `str` и объекты stdlib: жёстких
   зависимостей от Playwright, Selenium и HTTP-клиентов у модуля нет.

---

## 2. Что используют существующие системы

| Система | Что хранит | Формат |
|---|---|---|
| Crawlee JS v3 | `Session`: cookieJar, userData, errorScore, usageCount, expiresAt | tough-cookie JSON (`key` вместо `name`, ISO-даты); состояние пула — в KeyValueStore по ключу `SDK_SESSION_POOL_STATE` |
| Crawlee JS v4 (не выпущен) | то же плюс `proxyInfo` и `fingerprint {browser, platform, device}` | Apify пришла к модели «сессия = cookies + прокси + отпечаток» |
| Crawlee Python 1.11 | `SessionModel`: cookies (`CookieParam`), errorScore, usageCount, maxAge | pydantic с camelCase-алиасами; `CookieParam` — **snake_case** (`http_only`, `same_site`); есть `get_cookies_as_playwright_format()` |
| Scrapy 2.19 | jar в памяти, без персистентности | `Request(cookies=dict \| [ {name, value, domain, path, secure} ])` |
| browser-use 0.13 | `storage_state` | при автосохранении — сырые CDP-cookies; `origins[]` дополнен `sessionStorage` |
| Botasaurus | `profile.json` | сырые CDP-cookies и `local_storage` |
| nodriver / zendriver | `.session.dat` | **pickle** объектов CDP; чужой файл не загружать |
| AdsPower API | профиль: username, password, `fakey` (TOTP), cookie, прокси, `fingerprint_config` | cookies — JSON в стиле chrome.cookies или Netscape |
| GoLogin SDK | профиль: `navigator`, `proxy`, `timezone`, `webGLMetadata`, `fonts` | cookies в стиле chrome.cookies |
| Multilogin, Octo | импорт cookies в JSON и Netscape | точная схема не подтверждена |
| instagrapi | settings JSON: cookies `{name: value}`, uuids, device_settings, user_agent | платформенный формат |
| Telethon / Pyrogram | `.session` (SQLite), StringSession | платформенный формат, не трогаем |

Чему стоит поучиться у Crawlee: счётчики `errorScore`, `maxErrorScore`, `usageCount`,
`maxUsageCount` и срок жизни сессии. Аналоги в Identity Hub уже есть: `error_score`, квоты,
аренда. Модель «cookies + прокси + отпечаток» тоже совпадает с нашей: сессия, прокси и профиль.

---

## 3. Форматы cookie

### 3.1. Один и тот же cookie в разных форматах

Пример: доменная cookie `sid` для `example.com` и поддоменов, HttpOnly, Secure, `SameSite=Lax`,
истекает 2026-01-01.

**Playwright** (`context.cookies()`, `add_cookies()`, `storage_state`). Ключи в camelCase и в
Python тоже:
```json
{"name": "sid", "value": "abc", "domain": ".example.com", "path": "/",
 "expires": 1767225600, "httpOnly": true, "secure": true, "sameSite": "Lax"}
```

**CDP `Network.Cookie`** (Puppeteer, nodriver, Botasaurus, browser-use):
```json
{"name": "sid", "value": "abc", "domain": ".example.com", "path": "/",
 "expires": 1767225600, "size": 6, "httpOnly": true, "secure": true, "session": false,
 "sameSite": "Lax", "priority": "Medium", "sourceScheme": "Secure", "sourcePort": 443}
```

**Selenium / W3C WebDriver.** Поле называется `expiry`, значение **целое**:
```json
{"name": "sid", "value": "abc", "domain": ".example.com", "path": "/",
 "expiry": 1767225600, "httpOnly": true, "secure": true, "sameSite": "Lax"}
```

**chrome.cookies / Cookie-Editor / EditThisCookie / AdsPower / GoLogin:**
```json
{"domain": ".example.com", "expirationDate": 1767225600.0, "hostOnly": false,
 "httpOnly": true, "name": "sid", "path": "/", "sameSite": "lax", "secure": true,
 "session": false, "storeId": "0", "value": "abc"}
```
EditThisCookie добавляет `"id": 1`; Cookie-Editor пишет `storeId: null` и `sameSite: null`
вместо `"unspecified"`.

**Netscape `cookies.txt`** (curl, `MozillaCookieJar`, экспорт расширений). Семь полей через TAB:
```
# Netscape HTTP Cookie File
#HttpOnly_.example.com	TRUE	/	TRUE	1767225600	sid	abc
```

**`http.cookiejar.Cookie`** (основа httpx, requests, curl_cffi, tls-client):
```python
Cookie(0, "sid", "abc", None, False,
       ".example.com", True, True,          # domain, domain_specified, domain_initial_dot
       "/", True, True,                     # path, path_specified, secure
       1767225600, False, None, None,       # expires, discard, comment, comment_url
       {"HttpOnly": None, "SameSite": "Lax"})
```

**tough-cookie** (Crawlee JS). Имя в поле `key`, даты в ISO:
```json
{"key": "sid", "value": "abc", "domain": "example.com", "path": "/",
 "expires": "2026-01-01T00:00:00.000Z", "hostOnly": false, "httpOnly": true,
 "secure": true, "sameSite": "lax", "creation": "2025-10-01T10:00:00.000Z"}
```

**Crawlee Python `CookieParam`** (snake_case):
```python
{"name": "sid", "value": "abc", "domain": ".example.com", "path": "/",
 "expires": 1767225600, "http_only": True, "secure": True, "same_site": "Lax"}
```

**HAR 1.2.** Срок — ISO-строка, `sameSite` в спецификации нет:
```json
{"name": "sid", "value": "abc", "domain": ".example.com", "path": "/",
 "expires": "2026-01-01T00:00:00.000Z", "httpOnly": true, "secure": true}
```

### 3.2. Подводные камни

| Аспект | Как бывает | Что делаем |
|---|---|---|
| Регистр ключей | Playwright, CDP, Selenium, chrome.cookies — camelCase; Crawlee Python, zendriver — snake_case; tough-cookie — `key` вместо `name` | Внутри snake_case; на входе принимаем все варианты; на выходе — точно формат цели |
| Имя поля срока | `expires`, `expiry` (Selenium), `expirationDate` (chrome.cookies), `expires_utc` (Chrome SQLite) | Конвертер для каждого формата |
| Единицы срока | float секунд (Playwright, CDP, chrome.cookies); **целые** секунды (Selenium: float ChromeDriver отвергает); ISO-строка (tough-cookie, HAR); HTTP-дата (aiohttp Morsel); мс (Firefox SQLite, схема ≥ 16); мкс от 1601-01-01 (Chrome SQLite) | Внутри `float` секунд Unix UTC |
| Сессионная cookie | `-1` (Playwright, CDP); поле отсутствует (Selenium, chrome.cookies, CDP при записи); `session: true`; `0` (curl-файл, EditThisCookie Netscape); пустая строка (`MozillaCookieJar.save`); `null`; «сейчас + 1 сутки» (Cookie-Editor Netscape — искажение) | Внутри `expires=None`; **никогда** не храним `0` и `-1` |
| `0` в сроке | В CDP `0` означает уже истёкшую cookie; Playwright `0` пропускает без проверки; `MozillaCookieJar` считает `0` истёкшей и молча отбрасывает | При чтении Netscape `0` → сессионная; в CDP и Playwright `0` не пишем |
| sameSite | `"Strict"/"Lax"/"None"` (Playwright, CDP, Selenium); `"strict"/"lax"/"no_restriction"/"unspecified"` (chrome.cookies); `null` (Cookie-Editor); `"Default"` (Puppeteer); числа −1/0/1/2 (Chrome SQLite), 0/1/2/256 (Firefox); нет вообще (Netscape, HAR, httpx `set`) | Внутри `"strict" \| "lax" \| "none" \| None`, где `None` — не задан |
| «Не задан» → Lax | Chromium при чтении через Playwright превращает отсутствующий `sameSite` в `"Lax"` | Потеря на стороне Playwright; в `dump.playwright()` поле не пишем, если `None` |
| Host-only и ведущая точка | Точка в `domain` означает cookie для поддоменов; host-only — без точки (Playwright, CDP, Netscape) или явный `hostOnly` (chrome.cookies); aiohttp **отрезает** точку и хранит признак отдельно; stdlib `domain_specified=False`; rnet признак не отдаёт | Внутри `domain` без точки и в нижнем регистре плюс `host_only: bool` |
| Отрисовка host-only | Playwright, CDP: host-only надёжнее передавать через `url`; chrome.cookies `set()`: без `domain`; Selenium: без `domain`, после перехода на этот хост | Конвертер выбирает правильный способ для цели |
| Дубли имён | Одно имя на разных domain или path — нормальная ситуация; `dict {name: value}` теряет дубли (httpx `dict()`, requests `get_dict`, curl_cffi `get_dict`, primp, Scrapy dict) | Ключ cookie — `(domain, host_only, path, name, partition_key)` |
| HttpOnly в `http.cookiejar` | Хранится в `rest`, регистр ключа важен: `HttpOnly` (из Set-Cookie, httpx, requests), `HTTPOnly` (`MozillaCookieJar.load`), `httponly`, `http_only: "True"/"False"` (curl_cffi после ответа, **строкой**) | Читаем без учёта регистра, строку `"False"` понимаем как `False`; пишем `{"HttpOnly": None}` |
| HttpOnly по умолчанию | httpx `Cookies.set`, requests `create_cookie`, curl_cffi `Cookies.set` **всегда** ставят `HttpOnly` | Создаём `Cookie` stdlib напрямую, а не через `set()` |
| Потеря HttpOnly в Netscape | `MozillaCookieJar.save` пишет `#HttpOnly_`, только если ключ ровно `HTTPOnly` | Свой писатель Netscape, не `MozillaCookieJar.save` |
| Проверка точки в Netscape | `MozillaCookieJar.load`: точка и флаг поля 2 не совпадают → `LoadError` | Свой читатель, терпимый к несовпадению |
| Cookie без домена | httpx/requests `domain=""` и aiohttp `update_cookies` без `response_url` уходят **на любой хост**; curl_cffi прикрепляет её к хосту каждого запроса | Конвертеры всегда ставят домен; для aiohttp передают `response_url` |
| IP-адреса | aiohttp без `unsafe=True` не принимает cookie для IP | В подсказке к конвертеру aiohttp |
| Partitioned (CHIPS) | Playwright `partitionKey: str` (с 1.54); CDP и chrome.cookies — объект `{topLevelSite, hasCrossSiteAncestor}`; Puppeteer — `{sourceOrigin, …}`; в Selenium и Netscape нет | Внутри `partition_key: {top_level_site, has_cross_site_ancestor}`; где не поддерживается — cookie пропускаем с предупреждением, а не превращаем в обычную |
| Лишние поля | CDP: size, session, priority, sourceScheme, sourcePort, partitionKeyOpaque; chrome.cookies: storeId, id | На входе игнорируем; `priority`, `source_scheme`, `source_port` можно сохранить в `extras` для возврата в CDP |
| Префиксы `__Host-` и `__Secure-` | По RFC 6265bis: `__Secure-` требует Secure; `__Host-` — Secure, без Domain, `Path=/`; curl_cffi исправляет сам; Playwright не проверяет | Проверяем при создании модели (`ValidationFailed`) |
| Срок больше 400 дней | Chrome ограничивает 400 днями (схема v19); Playwright отвергает `expires > 253402300799` | Не обрезаем; в `dump.playwright()` проверяем верхнюю границу |
| `sessionStorage` | В Playwright `storage_state` его нет; browser-use добавляет в `origins[]` | Храним; в Playwright не отдаём (нет API), отдаём скриптом восстановления |
| Pickle | nodriver/zendriver `.session.dat`; aiohttp до 3.14.0 | Не загружаем: это выполнение кода. Конвертер работает с JSON (`to_json`) |
| Строка `Cookie: a=1; b=2` | Теряет domain, path, срок, флаги; `SimpleCookie.load` молча обрывается на значении-JSON и теряет дубли | Только как выход (`to_cookie_header(url)`) или вход с явным URL; парсер свой |

### 3.3. HTTP-клиенты: что принимают

| Клиент | Как передать cookie с полными атрибутами | Что теряется |
|---|---|---|
| httpx 0.28 | `httpx.Client(cookies=jar)`, где `jar` — `http.cookiejar.CookieJar` (используется **по ссылке**); cookies на запрос — deprecated | при `Cookies.set()` — secure, expires; HttpOnly ставится всегда |
| requests 2.34 | `session.cookies.set_cookie(cookie)` для каждой `Cookie` | `create_cookie` по умолчанию ставит HttpOnly; `rest` заменяется целиком |
| curl_cffi 0.16 | `Session(cookies=jar)` (по ссылке); jar передаётся в libcurl строками Netscape на каждом запросе | `sameSite` теряется при обратном пути; HttpOnly превращается в `rest={"http_only": "True"}` |
| tls-client 2.0 | как requests (`session.cookies` — копия RequestsCookieJar) | как requests |
| aiohttp 3.14 | `jar.update_cookies(SimpleCookie, response_url=URL("https://host/"))`; срок — HTTP-дата в `Morsel["expires"]` | без `response_url` cookie уходит на все хосты; точка в domain отрезается |
| primp 2.0 | `client.set_cookies(url, {name: value})` | всё, кроме имени и значения |
| rnet 3.0rc | `Jar.add(rnet.Cookie(...), url)` | host-only не отдаётся; `SameSite=None` не представлен |
| curl CLI / pycurl | файл Netscape (`-b file`, `-c file`) | `sameSite`; сессионные пишутся с `0` |

---

## 4. Предлагаемые модели

Модуль `identity_hub.formats`. Pydantic v2, как остальные схемы библиотеки. Модели можно
наследовать и расширять. Значения секретов скрыты в `repr` и логах (`SecretStr`).

### 4.1. Cookie

```python
class SameSite(StrEnum):
    STRICT = "strict"
    LAX = "lax"
    NONE = "none"          # «None» из заголовка; отсутствие атрибута — same_site=None

class PartitionKey(BaseModel):
    top_level_site: str                       # "https://top.example"
    has_cross_site_ancestor: bool | None = None

class Cookie(BaseModel):
    name: str
    value: SecretStr                          # значение в repr и логи не попадает
    domain: str                               # нижний регистр, без ведущей точки
    host_only: bool = False                   # True — только этот хост, без поддоменов
    path: str = "/"
    expires: float | None = None              # секунды Unix UTC; None — сессионная
    secure: bool = False
    http_only: bool = False
    same_site: SameSite | None = None         # None — атрибут не задан
    partition_key: PartitionKey | None = None # CHIPS
    created_at: float | None = None           # необязательно, для tough-cookie и CDP
    extras: dict[str, Any] = {}               # priority, source_scheme… для возврата в CDP

    @property
    def key(self) -> tuple[str, bool, str, str, str | None]: ...   # идентичность cookie
    def is_expired(self, now: float) -> bool: ...
    def matches(self, url: str) -> bool: ...   # RFC 6265: домен, путь, secure
```

Правила нормализации на входе:
- `domain`: срезать ведущую точку и перевести в нижний регистр. Точка или явный
  `hostOnly=false` означает `host_only=False`. Источник без признака и без точки (Playwright,
  CDP) даёт `host_only=True`, так их и трактует браузер.
- `expires`: значения `-1`, `0`, пустая строка, `null`, отсутствие поля, `session: true` и
  `"Infinity"` у tough-cookie → `None`. ISO-строка, HTTP-дата, миллисекунды и микросекунды
  от 1601 → float секунд.
- `same_site`: `"no_restriction"` → `none`, `"unspecified"`, `null` и `"Default"` → `None`,
  остальное — без учёта регистра.
- `http_only` из `rest`: ключ ищем без учёта регистра (`HttpOnly`, `HTTPOnly`, `httponly`,
  `http_only`); строку `"False"` понимаем как `False`.
- Проверки: `__Host-` и `__Secure-` по RFC 6265bis; `SameSite=None` без `secure` и
  `partition_key` без `secure` — предупреждение (браузер такую cookie отвергнет).

### 4.2. Сессия браузера

```python
class OriginStorage(BaseModel):
    origin: str                                # "https://example.com"
    local_storage: dict[str, str] = {}
    session_storage: dict[str, str] = {}       # расширение browser-use; Playwright не принимает

class Cookies(RootModel[list[Cookie]]):         # коллекция; ключ — Cookie.key
    def for_url(self, url: str) -> Cookies: ...
    def header_for(self, url: str) -> str: ...    # строка заголовка Cookie по RFC 6265
    def get(self, name: str, *, domain: str | None = None, path: str | None = None) -> Cookie | None: ...
    dump: ClassVar[CookiesDump]                  # неймспейсы форматов, раздел 5.0
    load: ClassVar[CookiesLoad]

class BrowserSession(BaseModel):
    cookies: Cookies = Cookies([])
    origins: list[OriginStorage] = []
    indexed_db: Any | None = None              # как отдал Playwright ≥ 1.51; схема не разобрана

    def expires_at(self, auth_cookies: Collection[str]) -> float | None: ...
    dump: ClassVar[SessionDump]
    load: ClassVar[SessionLoad]
```

`expires_at` — минимальный срок среди cookie, названных сервисом «авторизационными»
(например, `Service(session_auth_cookies={"sessionid"})`). Его можно передать в `expires_at`
сессии хаба, чтобы карточка показывала, когда сессия истечёт сама.

### 4.3. Классификация: креды и сессии

**Классифицируем по форме хранимых данных, а не по названию протокола.** Basic, Digest,
OAuth 2.0, OIDC, CAS и SAML — это способы получить или использовать доступ. Форм хранения у них
меньше, и отдельный тип нужен только там, где отличаются поля.

Две оси:
- **Креды** — долгоживущий секрет, из которого получают доступ: «что знаешь или имеешь».
  Хранятся в `credentials` по видам (`kind`/`name`, `D-214`).
- **Сессия** — то, что сервер выдал после входа. Протухает и обновляется. Хранится в `session`
  по слотам.

| Протокол или случай | Креды | Сессия | Почему так |
|---|---|---|---|
| Вход через форму | `PasswordCredentials` | `BrowserSession` | самый частый случай |
| Basic, Digest, NTLM | `PasswordCredentials` | — | сессии нет: логин и пароль уходят в каждом запросе; это способ передачи (`basic_auth()`), а не формат хранения |
| access + refresh (свой JWT) | `PasswordCredentials` или `ApiKey` | `TokenSession` | пара токенов — это токен-сессия, неважно, по OAuth2 или самописная |
| OAuth 2.0 | `OAuthClient` | `TokenSession` | `client_id`/`client_secret`, `token_url` — креды приложения; полученные токены — сессия |
| OpenID Connect | `OAuthClient` | `TokenSession` с `id_token` | OIDC — это OAuth 2.0 плюс `id_token`; отдельный тип дублировал бы поля. Чтение — `TokenSession.id_claims()` без проверки подписи |
| OAuth 1.0a | `OAuth1Client` | `OAuth1Session` | **отдельный тип нужен**: `token` + `token_secret`, запросы подписываются HMAC, нет срока и refresh (старый X/Twitter API, Garmin, Flickr) |
| JWT bearer grant (RFC 7523), сервисный аккаунт Google, Sign in with Apple | `PrivateKeyCredentials` | `TokenSession` | ключ подписывает утверждение, на выходе обычные токены |
| CAS | `PasswordCredentials` | `BrowserSession` | результат — cookie TGC на домене CAS-сервера плюс cookie сервиса; service ticket одноразовый и не хранится |
| SAML | `PasswordCredentials` | `BrowserSession` | assertion одноразовая, на выходе cookie-сессия |
| Подпись запросов HMAC (AWS SigV4, биржи) | `SigningKey` | — | секрет не уходит в сеть, им подписывается каждый запрос; сессии нет |
| Статический API-ключ | `ApiKey` | — | ключ уходит в заголовке или параметре каждого запроса |
| Свой заголовок после входа (`X-Auth-Token`) | `PasswordCredentials` | `HeaderSession` | не OAuth: сервер выдал непрозрачный заголовок со сроком |
| mTLS | `ClientCertificate` | — | сертификат и ключ используются на уровне TLS |
| 2FA | `OtpSecret`, `RecoveryCodes` | — | второй фактор при входе; коды восстановления одноразовые |
| Passkeys (WebAuthn) | позже | — | Playwright с 1.61 сохраняет их в `storage_state`; пока редкость, модель не вводим |
| Telegram MTProto, `.session`, tdata | схема приложения | схема приложения | платформенные форматы; общая модель не нужна |

Итоговое дерево:
```
Креды (credentials, по kind)        Сессии (session, по slot)
├─ PasswordCredentials              ├─ BrowserSession   ← форма, CAS, SAML, OIDC в браузере
├─ OtpSecret, RecoveryCodes         ├─ TokenSession     ← OAuth 2.0, OIDC (id_token), access/refresh
├─ ApiKey                           ├─ OAuth1Session    ← OAuth 1.0a
├─ SigningKey                       └─ HeaderSession    ← свой заголовок после входа
├─ OAuthClient, OAuth1Client
├─ PrivateKeyCredentials
└─ ClientCertificate
```

Два правила, которые из этого следуют:
1. **Почта для восстановления — не креды, а отдельный аккаунт.** Ящик — аккаунт сервиса `mail`
   со своим паролем, сессией и состоянием (бан, вход). Связывается с основным через уже
   существующие связи (`ImportRecord.links`, `ctx.linked("recovery_email")`). Если хранить его
   как вид кредов, пропадут состояние и проверки самого ящика. Поэтому модели `EmailAccess` нет.
2. **Смешанная сессия — это несколько слотов, а не один составной тип.** SPA часто держит и
   cookies, и bearer-токен API:
   ```python
   await lease.save_session(browser_state, slot="default")   # BrowserSession
   await lease.save_session(api_tokens, slot="api")          # TokenSession
   ```
   Схема сейчас одна на все слоты (`Service.session`). Нужна схема на слот, например
   `Service(session_slots={"api": TokenSession})`. Это проверяется и решается в `S6.1`.

### 4.4. Сессии API

```python
class TokenSession(BaseModel):                 # RFC 6749 §5.1 + абсолютный срок, как в Authlib
    access_token: SecretStr
    token_type: str = "Bearer"
    refresh_token: SecretStr | None = None
    scope: str | None = None
    id_token: SecretStr | None = None          # OIDC
    expires_at: float | None = None            # секунды Unix; из expires_in + момент получения

    def authorization_header(self) -> dict[str, str]: ...    # {"Authorization": "Bearer …"}
    def is_expired(self, now: float, *, leeway: float = 60) -> bool: ...
    def id_claims(self) -> dict[str, Any] | None: ...         # payload id_token без проверки подписи
    load: ClassVar[TokenLoad]                  # TokenSession.load.oauth(response, received_at=...)

class OAuth1Session(BaseModel):                # OAuth 1.0a (RFC 5849)
    token: SecretStr
    token_secret: SecretStr

class HeaderSession(BaseModel):                # непрозрачный заголовок, выданный после входа
    headers: dict[str, SecretStr]              # {"X-Auth-Token": "…"}
    expires_at: float | None = None
```

### 4.5. Credentials

```python
class PasswordCredentials(BaseModel):
    login: str                                  # логин, email или телефон — как требует платформа
    password: SecretStr
    email: str | None = None
    phone: str | None = None

    def basic_auth(self) -> dict[str, str]: ...  # {"Authorization": "Basic …"}

class OtpSecret(BaseModel):                     # Key Uri Format: TOTP и HOTP
    type: Literal["totp", "hotp"] = "totp"
    secret: SecretStr                           # base32 без "="
    issuer: str | None = None
    account: str | None = None
    algorithm: Literal["SHA1", "SHA256", "SHA512"] = "SHA1"
    digits: Literal[6, 8] = 6
    period: int = 30                            # только TOTP
    counter: int | None = None                  # только HOTP, обязателен для него

    @classmethod
    def from_uri(cls, uri: str) -> Self: ...    # otpauth://totp/Issuer:acc?secret=...&issuer=...
    def to_uri(self) -> str: ...                # параметры по умолчанию опускаются, как в pyotp
    def code(self, at: float) -> str: ...       # RFC 6238 / RFC 4226 на stdlib hmac, без pyotp

class RecoveryCodes(BaseModel):
    codes: list[SecretStr]
    used: frozenset[int] = frozenset()          # индексы израсходованных кодов

    def take(self) -> tuple[str, Self]: ...     # первый свободный код и новая версия модели

class ApiKey(BaseModel):
    key: SecretStr
    secret: SecretStr | None = None

class SigningKey(BaseModel):                    # HMAC-подпись запросов
    key_id: str
    secret: SecretStr
    algorithm: Literal["HMAC-SHA256", "HMAC-SHA512"] = "HMAC-SHA256"

class OAuthClient(BaseModel):                   # регистрация приложения OAuth 2.0 / OIDC
    client_id: str
    client_secret: SecretStr | None = None      # None — публичный клиент (PKCE)
    token_url: str | None = None
    authorize_url: str | None = None
    scopes: list[str] = []
    redirect_uri: str | None = None

class OAuth1Client(BaseModel):
    consumer_key: str
    consumer_secret: SecretStr

class PrivateKeyCredentials(BaseModel):         # JWT bearer grant, сервисные аккаунты
    key_id: str | None = None
    private_key: SecretStr                      # PEM
    algorithm: Literal["RS256", "ES256", "EdDSA"] = "RS256"
    issuer: str | None = None                   # client_email у Google, team_id у Apple

class ClientCertificate(BaseModel):             # mTLS
    cert: str                                   # PEM, не секрет
    key: SecretStr                              # PEM
    passphrase: SecretStr | None = None
```

Модели ложатся на существующие виды credentials (`kind`/`name`, `D-214`): `default` —
`PasswordCredentials`, `totp` — `OtpSecret`, `recovery` — `RecoveryCodes`, `api` — `ApiKey`,
`signing` — `SigningKey`, `oauth_client` — `OAuthClient`. Имена видов — рекомендация, а не
требование: виды по-прежнему объявляет сервис в `credential_kinds`.

Чего модели **не** делают:
- не выполняют сетевых запросов (обмен кода на токен, refresh) — это `Authenticator` приложения;
- не проверяют подпись `id_token`: для этого нужны ключи провайдера (JWKS) и сеть;
- не реализуют подпись OAuth 1.0a и HMAC-запросов: у каждого API свой канонический вид запроса.

### 4.6. Отпечаток браузера (в профиле аккаунта)

```python
class BrowserFingerprint(BaseModel):
    # кто
    user_agent: str
    browser: Literal["chrome", "firefox", "safari", "edge"]
    browser_version: str                        # "130.0.6723.92"
    os: Literal["windows", "macos", "linux", "android", "ios"]
    os_version: str | None = None
    device: Literal["desktop", "mobile", "tablet"] = "desktop"
    platform: str | None = None                 # navigator.platform: "Win32", "MacIntel"
    user_agent_data: UserAgentData | None = None # client hints: brands, fullVersionList, mobile…
    # окружение
    locale: str | None = None                   # "en-US"
    languages: list[str] = []                   # navigator.languages
    timezone: str | None = None                 # IANA: "Europe/Berlin"
    geolocation: Geolocation | None = None
    # экран и железо
    screen: Screen | None = None                # width, height, avail_*, color_depth, device_pixel_ratio
    viewport: Viewport | None = None
    hardware_concurrency: int | None = None
    device_memory: float | None = None
    max_touch_points: int | None = None
    webgl: WebGl | None = None                  # vendor, renderer
    fonts: list[str] = []
    webrtc: Literal["real", "proxy", "disabled"] | None = None
    headers: dict[str, str] = {}                # Accept-Language, sec-ch-ua…
    # исходник для повторной выдачи без потерь
    source: Literal["browserforge", "fingerprint-suite", "manual"] | None = None
    raw: dict[str, Any] | None = None
```

Почему профиль, а не сессия:
- платформы привязывают cookie к устройству: смена UA при повторном входе подозрительна, а
  сессия при входе заменяется целиком;
- отпечаток не секрет, профиль хранится открыто, и по `browser` или `os` можно фильтровать
  пулы (`filterable_profile`).

Пример схемы профиля сервиса:
```python
class ShopProfile(BaseModel):
    display_name: str | None = None
    fingerprint: BrowserFingerprint
```

### 4.7. Прокси

Прокси в Identity Hub уже есть (`proxies.*`, URL `scheme://user:pass@host:port`, свой
`ProxyCreate`). Новых моделей не нужно, нужны только конвертеры URL (раздел 5.6).

---

## 5. Конвертеры

### 5.0. Форма API: неймспейсы `dump` и `load`

Конвертация вызывается прямо на моделях, импортировать модули форматов не нужно:

```python
state = session.dump.playwright()                 # → StorageState (dict)
rows  = session.cookies.dump.selenium()           # → list[dict], expiry:int
jar   = session.cookies.dump.cookiejar()          # → http.cookiejar.CookieJar
text  = session.cookies.dump.netscape()           # → str

session = BrowserSession.load.playwright(state)   # → BrowserSession
cookies = Cookies.load.netscape(text)             # → Cookies
cookies = Cookies.load(uploaded_bytes)            # автоопределение формата
```

Почему так:
- **Имена.** Пара `dump`/`load` симметрична и устоялась в Python для «внешнее представление ↔
  объект»: `json`, `pickle`, `yaml`, marshmallow (`schema.load(dict)`, `schema.dump(obj)`). На вход
  приходит не только текст, но и `dict`, и `CookieJar`, поэтому `parse` не подходит. Пары
  `export`/`import` и `to`/`from` упираются в ключевые слова Python.
- **Неймспейсы, а не плоские `to_*`.** При 15–20 форматах модель не разрастается, автодополнение
  работает по шагам (`cookies.dump.` → список форматов), обратные операции называются одинаково
  (`dump.netscape` ↔ `load.netscape`).
- **Не строковый `to("playwright")`.** У каждого метода точный тип результата, mypy его видит.

Устройство:
- **Логика — в модулях** `identity_hub.formats.<формат>` с функциями `dump(obj, **opts)` и
  `load(data, **opts)`. Методы неймспейсов вызывают их в одну строку. Модули публичные: ими
  пользуются для сырых данных без модели и для своих форматов.
- **Неймспейсы — дескрипторы.** У pydantic-модели объявляются как `ClassVar`, чтобы не стать
  полями. `load` вызываемый: вызов без формата — автоопределение.
- **На модели — только операции над данными:** `Cookies.for_url(url)`, `Cookies.header_for(url)`
  (строка заголовка `Cookie` — это операция по URL, а не формат), `Cookies.get(...)`,
  `BrowserSession.expires_at(...)`.
- **Живые инструменты — методы аренды:** `lease.playwright_context(browser)` и
  `lease.selenium_apply(driver)` (раздел 6). Они не конвертируют, а открывают контекст или
  добавляют cookie в браузер, им нужны прокси и сохранение сессии.
- **Потери при конвертации** — Python-предупреждение `FormatLossWarning` («sameSite не
  поддерживается Netscape», «partitioned cookie пропущена для Selenium»). С `strict=True` —
  `ValidationFailed`. Тип результата от этого не меняется.
- **Зависимостей нет:** `dump` возвращает `dict`, `list`, `str` и объекты stdlib, модули не
  импортируют Playwright, Selenium и HTTP-клиенты.

Ниже `C` — `Cookies`, `S` — `BrowserSession`, `F` — `BrowserFingerprint`.

### 5.1. Браузеры

| Вызов | Детали |
|---|---|
| `C.dump.playwright()` / `C.load.playwright(rows)` | camelCase; `expires=None` → `-1`; host-only — `domain` без точки, доменная — `.domain`; при `same_site=None` поле не пишем; `partitionKey` — строкой `top_level_site` |
| `S.dump.playwright()` / `S.load.playwright(state)` | `{cookies, origins:[{origin, localStorage:[{name,value}]}]}`; `sessionStorage` не пишем (`FormatLossWarning`); `indexedDB` — как есть |
| `C.dump.cdp()` / `C.load.cdp(rows)` | `Network.CookieParam`: host-only через `url`, сессионная — без `expires`; `partitionKey` — объект; `extras` возвращаются (`priority`, `sourceScheme`); при загрузке `session: true` или `expires == -1` → `None`, отсутствующий `sameSite` → `None` |
| `C.dump.puppeteer()` | как CDP, но `partitionKey = {sourceOrigin, hasCrossSiteAncestor}` |
| `C.dump.selenium()` / `C.load.selenium(rows)` | `expiry` — `int`, у сессионной поля нет; `sameSite` с заглавной; partitioned пропускаются |
| `C.dump.chrome_extension()` / `C.load.chrome_extension(rows)` | `expirationDate`, `hostOnly`, `session`, `storeId`; `sameSite`: `none` → `"no_restriction"`, `None` → `"unspecified"`; при загрузке терпим `id`, `storeId: null`, `sameSite: null`. Тот же формат у Cookie-Editor, EditThisCookie, AdsPower, GoLogin |
| `C.dump.crawlee()` / `C.load.crawlee(rows)` | Crawlee Python `CookieParam`: snake_case, `same_site` с заглавной |
| `C.dump.tough_cookie()` / `C.load.tough_cookie(jar)` | Crawlee JS: `key`, ISO-даты, `"Infinity"`, `hostOnly`, `creation`; пропущенные поля — по умолчанию |

### 5.2. HTTP-клиенты

| Вызов | Что даёт | Как использовать |
|---|---|---|
| `C.dump.cookiejar()` | `http.cookiejar.CookieJar` с `Cookie` stdlib, созданными напрямую: доменная — `.domain` и `domain_specified=True`, host-only — `domain_specified=False`; `rest={"HttpOnly": None, "SameSite": "Lax"}` | `httpx.Client(cookies=jar)`, `curl_cffi.Session(cookies=jar)`, `requests.Session().cookies.update(jar)`, tls-client |
| `C.load.cookiejar(jar)` | `Cookies` | разбор `rest` без учёта регистра, `http_only: "True"/"False"` от curl_cffi; `discard` или `expires is None` → сессионная |
| `C.dump.aiohttp()` | пары (`SimpleCookie`, `response_url`) по хосту | `for m, url in pairs: jar.update_cookies(m, URL(url))`; срок — HTTP-датой; для IP — `CookieJar(unsafe=True)` |
| `C.header_for(url)` | строка заголовка `Cookie` | фильтр по RFC 6265 (домен, путь, secure, срок), длинный путь первым; для primp и сырых запросов. Операция, а не формат |
| `C.load.set_cookie(headers, url=, received_at=)` | `Cookies` | свой разбор `Set-Cookie`, без `SimpleCookie`: `Max-Age` относительно `received_at`, host-only по отсутствию `Domain`, путь по умолчанию из URL |
| `C.dump.scrapy()` | `[{name, value, domain, path, secure}]` | Scrapy `Request(cookies=...)`. `dict {name: value}` не делаем — он теряет дубли |

### 5.3. Файлы

| Вызов | Детали |
|---|---|
| `C.dump.netscape(session_expires="zero")` / `C.load.netscape(text)` | Свой читатель и писатель вместо `MozillaCookieJar`. `#HttpOnly_` пишем, флаг `TRUE` — строго при точке. Сессионную cookie по умолчанию пишем как `0` (так делает curl); `"empty"` — для `MozillaCookieJar`. При загрузке `0` и пустое поле → сессионная. Терпим несовпадение точки и флага, CRLF, `#` в комментариях (кроме `#HttpOnly_`). `FormatLossWarning`: `sameSite` потерян |
| `C.load.har(har)` | `log.entries[].response.cookies`: ISO-срок, без `sameSite`; домен из записи или URL запроса |
| `C.load(data)`, `S.load(data)` | Автоопределение по содержимому: JSON-массив (поля `expirationDate`, `expiry`, `expires`, `key`), объект `storage_state`, Netscape по заголовку или 7 TAB-полям, строка `Cookie`, base64 от любого из них. Нужен для импорта «как прислал продавец» |

Чего не делаем:
- **Chrome `Cookies` SQLite.** Значения зашифрованы, на Windows с Chrome 127 — App-Bound Encryption.
- **pickle-файлы nodriver.** Загрузка — это выполнение кода.

Firefox `cookies.sqlite` не зашифрован, его можно добавить позже как `C.load.firefox_sqlite(path)`:
срок в мс для схемы ≥ 16 (определяется по `PRAGMA user_version`), `sameSite` 0/1/2/256.

### 5.4. Отпечаток

| Вызов | Детали |
|---|---|
| `F.load.browserforge(fp)` / `F.load.fingerprint_suite(json)` | `navigator.userAgent`, `userAgentData`, `language(s)`, `platform`, `hardwareConcurrency`, `deviceMemory`, `screen.*`, `videoCard{vendor, renderer}`, `fonts`, `headers`; исходник сохраняется в `raw`. У browserforge `headers` внутри отпечатка, у fingerprint-suite — рядом |
| `F.dump.playwright_context(proxy=)` | `user_agent`, `locale`, `timezone_id`, `viewport`, `screen`, `device_scale_factor`, `is_mobile`, `has_touch`, `geolocation`, `extra_http_headers`, `proxy={server, username, password}` |
| `F.dump.camoufox()` | плоский dict: `navigator.userAgent`, `screen.width`, `webGl:vendor`, `timezone`, `locale:language`, `locale:region`, `geolocation:latitude`… `FormatLossWarning`, если отпечаток не от Firefox |
| `F.dump.impersonate(client)` | цель TLS-профиля для `"curl_cffi"`, `"tls_client"`, `"primp"`, `"rnet"`. Берётся **ближайшая доступная версия того же браузера не новее UA**: например, `chrome131` в curl_cffi 0.16.3 нет, ближайшая — `chrome124`. Расхождение версии UA и TLS-профиля — сигнал детекта, поэтому выдаётся `FormatLossWarning`. Таблицы целей версионируются вместе с поддерживаемой версией клиента |
| `F.dump.adspower()` / `F.dump.gologin()` | `ua`, `screen_resolution`, `webgl_config.unmasked_vendor/renderer`, `timezone`, `language`; у GoLogin `resolution` — строка `"WxH"` |

### 5.5. Credentials

| Вызов | Детали |
|---|---|
| `OtpSecret.from_uri(uri)` / `secret.to_uri()` / `secret.code(at)` | Key Uri Format для TOTP и HOTP. Google Authenticator игнорирует `algorithm` и `period`, поэтому при не-SHA1 или не-30 — предупреждение. `from_uri`/`to_uri` остаются обычными методами: формат у OTP один |
| `PasswordCredentials.basic_auth()`, `TokenSession.authorization_header()` | заголовок `Authorization` для Basic и Bearer |
| `TokenSession.id_claims()` | payload `id_token` (OIDC) без проверки подписи |
| `TokenSession.load.oauth(response, received_at=)` | RFC 6749: `expires_at = received_at + expires_in` |
| `formats.lines.load_account(line, template, sep)` | Строки продавцов: шаблон полей и разделитель, например `"login:password:email:email_password:totp"`, `sep=":"` или `"\|"`. Поля `cookies` и `ua` — через `Cookies.load`. Стандарта нет, поэтому только шаблоны, без угадывания порядка |

### 5.6. Прокси

Аренда получает свойство `lease.proxy` → `ProxyUrl | None` с неймспейсом `dump`. Строка
`lease.proxy_url` остаётся как раньше.

| Вызов | Результат |
|---|---|
| `lease.proxy.dump.playwright()` | `{"server": "socks5://host:port", "username": ..., "password": ...}` |
| `lease.proxy.dump.httpx()` | `url` для `httpx.Client(proxy=...)`; аргумент `proxies` удалён в httpx 0.28 |
| `lease.proxy.dump.requests()` | `{"http": url, "https": url}` |
| `lease.proxy.dump.aiohttp()` | `(proxy_url_без_пароля, BasicAuth)` |
| `formats.lines.load_proxy(line, template)` | `host:port:user:pass`, `protocol;host;port;login;password` (Octo) — по шаблону |

---

## 6. Как это выглядит в сервисе

```python
from identity_hub import Service
from identity_hub.formats import BrowserFingerprint, BrowserSession, Cookies, PasswordCredentials, OtpSecret

class ShopProfile(BaseModel):
    fingerprint: BrowserFingerprint

SHOP = Service(
    name="shop",
    credentials=PasswordCredentials,
    credential_kinds={"totp": OtpSecret},
    session=BrowserSession,
    session_auth_cookies=frozenset({"sessionid"}),
    profile=ShopProfile,
)
```

Браузер — одним методом аренды: отпечаток, прокси и сессия подставляются сами, обновлённые
cookies сохраняются при выходе:
```python
async with pool.lease() as lease:
    async with lease.playwright_context(browser) as context:
        page = await context.new_page()
        ...
```

То же вручную, если нужен свой контекст:
```python
async with pool.lease() as lease:
    session = await lease.session()                       # BrowserSession
    fp = lease.identity.profile.fingerprint
    context = await browser.new_context(
        **fp.dump.playwright_context(proxy=lease.proxy_url),
        storage_state=session.dump.playwright(),
    )
    ...
    await lease.save_session(BrowserSession.load.playwright(await context.storage_state()))
```

Тот же аккаунт без браузера:
```python
async with httpx.AsyncClient(
    cookies=session.cookies.dump.cookiejar(),
    proxy=lease.proxy.dump.httpx(),
    headers={"User-Agent": fp.user_agent},
) as client:
    ...

r = curl_cffi.requests.get(url, cookies=session.cookies.dump.cookiejar(),
                           impersonate=fp.dump.impersonate("curl_cffi"))
```

Selenium:
```python
await lease.selenium_apply(driver)    # переход на каждый домен и add_cookie
```

Импорт cookies «как прислали»:
```python
session = BrowserSession(cookies=Cookies.load(uploaded_bytes))
await space.imports.run(service=SHOP, records=[ImportRecord(external_ref=..., sessions={"default": session})], actor="intake")
```

---

## 7. Тесты, без которых модуль не стоит выпускать

1. **Золотые образцы.** Реальные выгрузки каждого формата в `tests/fixtures/formats/`:
   Playwright `storage_state`, CDP `Storage.getCookies`, Selenium `get_cookies()`, Cookie-Editor
   JSON и Netscape, EditThisCookie, файл curl, `MozillaCookieJar.save`, tough-cookie, Crawlee
   Python, HAR, aiohttp JSON. Каждый разбирается в ожидаемую модель.
2. **Круговые проверки.** Модель → формат → модель без потерь там, где формат их не теряет.
   Где теряет — потеря ровно та, что заявлена в предупреждении.
3. **Подводные камни отдельными тестами:**
   - `expires = 0` в Netscape;
   - `sameSite` unspecified;
   - host-only против доменной cookie;
   - одно имя на двух путях;
   - `http_only: "False"` от curl_cffi;
   - `HTTPOnly`/`HttpOnly` в `rest`;
   - partitioned cookie в Selenium;
   - `__Host-` с `Domain`.
4. **Интеграция** (необязательный маркер, если пакет установлен):
   - Playwright `add_cookies` + `cookies()`;
   - httpx и curl_cffi против локального сервера: cookie уходит только на свой домен;
   - aiohttp с `response_url`.
5. **Секреты.** `repr`, логи и сообщения об ошибках не содержат значений cookie, токенов и
   паролей.

---

## 8. Как разбить на задачи

Если брать в план, то отдельной фазой. Хранение и миграции не меняются: модели ложатся в
существующие `credentials`, `session` и `profile` сервиса.

1. **Модели и нормализация:** `Cookie`, `BrowserSession`, сессии API и креды по классификации
   раздела 4.3, `BrowserFingerprint`, схема сессии на слот, проверки RFC 6265bis.
2. **Конвертеры браузеров:** Playwright, CDP и Puppeteer, Selenium, chrome.cookies, Crawlee,
   tough-cookie.
3. **Конвертеры HTTP:** `http.cookiejar`, aiohttp, заголовки `Cookie` и `Set-Cookie`, Scrapy.
4. **Файлы и автоопределение:** Netscape, HAR, `Cookies.load(data)`, `formats.lines`
   (строки аккаунтов и прокси по шаблону).
5. **Отпечаток:** импорт browserforge и fingerprint-suite, выход в Playwright, Camoufox,
   `impersonate`, AdsPower и GoLogin.
6. **Интеграция с хабом и документация:** `Service(session_auth_cookies=...)` и срок сессии из
   cookie, рецепт «браузерный аккаунт» в `12-recipes.md`, CHANGELOG.

---

## 9. Не подтверждено

- Схема `origins[].indexedDB` в `storage_state` Playwright: храним как есть.
- Когда в CDP `partitionKey` сменил тип со строки на объект. В текущем протоколе — объект.
- Точная схема импорта cookies в Multilogin, Octo, Dolphin{anty}, Incogniton. Судя по описаниям,
  они принимают JSON в стиле chrome.cookies и Netscape.
- Как Selenium-драйверы обрабатывают ведущую точку в `domain` и неизвестный ключ `expires`.
- Что CDP `setCookies` делает с `__Host-`, `SameSite=None` без Secure и partitioned без Secure:
  отвергает или молча не ставит.
- `sameSite` в HAR-экспортах браузеров (в спецификации HAR 1.2 его нет).
- Отправит ли stdlib cookie с `expires=0`, загруженную с `ignore_expires=True`. По коду — нет.
- Формат cookies в noble-tls.
- В какой версии Camoufox перешёл с browserforge на fpgen. В актуальной версии приём объекта
  browserforge не гарантирован, поэтому выход в Camoufox — через свой маппер.
- Строки продавцов (`login:password:...`, `UA|cookies|token`) не стандартизированы, поэтому
  разбираем только по шаблону.

---

## 10. Версии и источники

Версии на 2026-10-15:

| Группа | Версии |
|---|---|
| Браузерная автоматизация | Playwright 1.64; Puppeteer 25.14; CDP tip-of-tree; zendriver 0.17.1; browser-use 0.13.11 |
| Расширения | Cookie-Editor 1.13 |
| Хранилища браузеров | Chrome `Cookies` схема 24; Firefox `cookies.sqlite` схема 17 |
| HTTP-клиенты | httpx 0.28.1; requests 2.34.2; aiohttp 3.14.5; curl_cffi 0.16.3; tls-client 2.0.0; primp 2.0.1; rnet 2.4.2 / 3.0.0rc22; curl 8.18 |
| Python | 3.13 / 3.14 |
| Сессии и краулинг | Crawlee JS 3.18.2; Crawlee Python 1.11.0; tough-cookie 6.0.2; Scrapy 2.19; scrapy-playwright 0.0.48 |
| Отпечатки | fingerprint-suite 2.1.89; browserforge 1.2.4; Camoufox 0.5.8 |
| Credentials | Authlib 1.9.1; pyotp 2.10 |

Поведение HTTP-клиентов и `http.cookiejar` проверено запуском кода. Остальное — по исходникам
и документации.

Основные источники:
- Playwright BrowserContext: https://playwright.dev/docs/api/class-browsercontext; исходник
  `packages/playwright-core/src/server/network.ts`
- CDP Network: https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Network.pdl
- Puppeteer: https://pptr.dev/api/puppeteer.cookie, https://pptr.dev/api/puppeteer.cookieparam
- W3C WebDriver Cookies: https://w3c.github.io/webdriver/#cookies
- chrome.cookies: https://developer.chrome.com/docs/extensions/reference/api/cookies
- Cookie-Editor: github.com/Moustachauve/cookie-editor (`interface/lib/jsonFormat.js`, `netscapeFormat.js`)
- EditThisCookie: github.com/ETCExtensions/Edit-This-Cookie (`js/cookie_helpers.js`)
- Chrome SQLite: chromium `net/extras/sqlite/sqlite_persistent_cookie_store.cc`
- Firefox SQLite: firefox `netwerk/cookie/CookiePersistentStorage.cpp`
- curl cookie file: https://curl.se/docs/http-cookies.html
- `http.cookiejar`: https://docs.python.org/3/library/http.cookiejar.html
- httpx: https://www.python-httpx.org/api/#cookies
- requests: https://requests.readthedocs.io/en/latest/api/#cookies
- aiohttp: https://docs.aiohttp.org/en/stable/client_reference.html#cookiejar
- curl_cffi: https://github.com/lexiforest/curl_cffi
- HAR 1.2: https://webperfwg.org/specs/HAR/Overview.html
- Crawlee: github.com/apify/crawlee (`packages/core/src/session_pool`), github.com/apify/crawlee-python (`src/crawlee/sessions`)
- tough-cookie: github.com/salesforce/tough-cookie (`lib/cookie/cookie.ts`)
- Scrapy cookies: https://docs.scrapy.org/en/latest/topics/cookies.html
- fingerprint-suite: github.com/apify/fingerprint-suite; browserforge: github.com/daijro/browserforge
- Camoufox: https://camoufox.com/python/usage/
- AdsPower Local API: https://localapi-doc-en.adspower.com/
- GoLogin: https://github.com/gologinapp/gologin
- Key Uri Format: https://github.com/google/google-authenticator/wiki/Key-Uri-Format
- RFC 6749 §5.1: https://www.rfc-editor.org/rfc/rfc6749#section-5.1
- RFC 6265bis (префиксы cookie): https://datatracker.ietf.org/doc/draft-ietf-httpbis-rfc6265bis/
