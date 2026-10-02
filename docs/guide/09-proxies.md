# 9. Прокси

Платформы связывают аккаунты по IP. Identity Hub хранит прокси, выдаёт их вместе с арендой,
закрепляет за аккаунтами и следит за их здоровьем. Сетевые запросы через прокси делает ваш код.

## Добавление прокси

```python
proxy, created = await space.proxies.ensure(
    "socks5://user:pass@1.2.3.4:1080", group="residential-de", max_identities=3
)

result = await space.proxies.create_many(
    ["socks5://u:p@1.2.3.5:1080", "http://u:p@1.2.3.6:8080"],
    group="residential-de",
    max_identities=3,
    on_conflict="skip",          # уже добавленные — в result.skipped
)
page = await space.proxies.list(group="residential-de", status="alive")   # с числом привязанных identity
proxy = await space.proxies.get(proxy.id)                                  # без логина и пароля
```

- Схемы: `http`, `https`, `socks4`, `socks5`, `socks5h`, `mtproxy`.
- Логин и пароль шифруются; хост и порт хранятся открыто.
- Дубликаты определяются по `схема + хост + порт + логин` и не создаются повторно.
- `max_identities` — сколько аккаунтов можно **закрепить** за прокси.
- Прокси из `ImportRecord.proxy` при импорте добавляются в пул `proxy_policy.pool` сервиса.

## Как прокси выдаётся аккаунту

Политика задаётся в сервисе или переопределяется в пуле:

```python
from identity_hub import ProxyDownPolicy, ProxyPolicy

TELEGRAM = Service(
    ...,
    proxy_policy=ProxyPolicy(
        group="residential-de",
        sticky=True,                              # аккаунт всегда ходит через один прокси
        max_identities=None,                      # None — брать max_identities самого прокси
        on_proxy_down=ProxyDownPolicy.WAIT,       # прокси упал: WAIT — не выдавать аккаунт, REBIND — перепривязать
    ),
)

async with pool.lease() as lease:
    proxy_url = lease.proxy.reveal() if lease.proxy else None   # строка с логином и паролем — не логировать
    client = make_client(proxy=proxy_url)
```

`lease.proxy` — `SecretValue`: в `repr`, `str` и f-строках пароль не виден. Вне аренды URL выдаёт
`await space.proxies.url(proxy_id, actor="ops:ivan", reason="проверка канала")`; раскрытие пишет
событие `proxy.secret.revealed` в той же транзакции, `actor` и `reason` обязательны.

```mermaid
flowchart TD
    A(["аренда аккаунта"]) --> S{"sticky?"}
    S -- нет --> N["наименее загруженный alive-прокси<br/>на эту аренду"]
    S -- да --> B{"есть привязка?"}
    B -- нет --> C["привязать alive-прокси<br/>с свободной ёмкостью"]
    B -- да --> H{"прокси alive?"}
    H -- да --> U["использовать привязку"]
    H -- нет --> P{"on_proxy_down"}
    P -- WAIT --> W["аккаунт не выдаётся,<br/>пока прокси не поднимется"]
    P -- REBIND --> C
    C -- "нет свободных" --> X["событие proxy.pool.exhausted,<br/>аккаунт не выдаётся"]
```

Событие `proxy.pool.exhausted` внешнее и `critical`, поэтому оно пишется экономно: одно на
вызов `lease()`/`acquire()`, сколько бы тот ни ждал, и не чаще одного в минуту на пул в
каждом процессе.

| Режим | Когда |
|---|---|
| `sticky=True, WAIT` | Платформа чувствительна к смене IP (Telegram, соцсети). Лучше подождать, чем засветить аккаунт на новом IP |
| `sticky=True, REBIND` | Смена IP допустима, но постоянный IP предпочтителен |
| `sticky=False` | IP не важен (парсинг публичных данных); ёмкость прокси в этом режиме не ограничивается |

Ручная перепривязка: `await space.proxies.rebind(identity_id, new_proxy_id, reason="...", actor="admin")`.

## Здоровье прокси

```mermaid
stateDiagram-v2
    [*] --> alive
    alive --> quarantined : error_score ≥ error_threshold
    quarantined --> alive : 2 успешные проверки подряд
    quarantined --> dead : dead_after неудачных проверок
    dead --> alive : ручная проверка (2 успеха) / override
```

- Ошибки прокси приходят от классификатора ошибок (`Fault(Blame.PROXY)`) или напрямую:
  `await space.proxies.report_failure(proxy_id, weight=1.0)`.
- Успехи уменьшают счёт: `await space.proxies.report_success(proxy_id)` — библиотека сама его
  не вызывает.
- Порог и backoff задаёт `IdentityHub(proxy_health=ProxyHealthPolicy(error_threshold=3.0,
  base_backoff=timedelta(seconds=60), max_backoff=timedelta(hours=1), dead_after=5))`.

**Перепроверку запускает ваш код** — в обслуживание она не входит:

```python
async def check(url: str) -> bool:
    async with httpx.AsyncClient(proxy=url, timeout=10) as c:
        return (await c.get("https://api.ipify.org")).status_code == 200

async def proxy_loop(space):
    while True:
        await space.proxies.recheck_due(check, limit=50)     # quarantined, у которых наступило время проверки
        await asyncio.sleep(60)

await space.proxies.recheck(proxy_id, check, actor="admin")                              # в том числе dead
await space.proxies.override(proxy_id, "alive", reason="заменили", actor="admin")       # вручную
```

Обзор: `await space.admin.proxy_overview()` — статусы, загрузка, ёмкость пулов;
`await space.admin.proxy_detail(proxy_id)` — привязанные аккаунты и история.

Дальше: [события и уведомления](10-events.md).
