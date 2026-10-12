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

Пачка из своей таблицы — структурными записями, со своим `id` и сопоставлением результата:

```python
from identity_hub import ProxyCreate

result = await space.using(db).proxies.create_many(
    [
        ProxyCreate("http", "gate.example.com", 8000, username="u1", password="p1", id=row1.id),
        ProxyCreate("http", "gate.example.com", 8000, username="u2", password="p2", id=row2.id),
        "socks5://u:p@1.2.3.7:1080",                      # URL и записи можно смешивать
    ],
    group="residential-de",
    on_conflict="skip",
)
for item in result.items:                                 # в порядке входа
    print(item.index, item.id, item.created)
```

- `result.items[i]` — исход записи `i`: id прокси и создан ли он этой записью. Адрес, который
  уже есть в пространстве или встретился в пачке раньше, даёт id существующего прокси и
  `created=False`; при `on_conflict="fail"` (по умолчанию) такая пачка откатывается целиком.
- `ProxyCreate.id` задаёт id прокси — удобно, когда источник правды о прокси — таблица
  приложения. `max_identities` записи перекрывает значение параметра.
- В пачке до 1000 записей; число запросов к БД от её размера не зависит.

Изменить и удалить:

```python
await space.proxies.replace_credentials(proxy.id, username="user", password="new-pass", actor="ops:ivan")
await space.proxies.move(proxy.id, group="residential-fr", actor="ops:ivan")
await space.proxies.delete(proxy.id, actor="ops:ivan")                # с привязками — ValidationFailed
await space.proxies.delete(proxy.id, actor="ops:ivan", unbind=True)   # отвязать аккаунты и удалить
```

- `replace_credentials` меняет только логин и пароль: адрес, статус и привязки аккаунтов
  остаются. Если прокси с тем же адресом и новым логином уже есть — `AlreadyExists`.
  `ensure()` с другим паролем по-прежнему возвращает существующую запись и пароль не меняет.
- `move` переносит прокси в другую группу; уже привязанные аккаунты остаются на нём.
- `delete` возвращает число отвязанных аккаунтов. Отвязанный аккаунт при следующей аренде
  получит прокси по политике сервиса заново.
- Изменения пишут события `proxy.updated` и `proxy.deleted`; в них нет логина и пароля.

Пачкой — одной транзакцией, до 1000 прокси:

```python
deleted = await space.proxies.delete_many(ids, actor="ops:ivan", unbind=True)
print(len(deleted.applied), deleted.unbound)          # удалено прокси, отвязано аккаунтов
for rejection in deleted.rejected:                    # отказ одной строки не отменяет остальные
    print(rejection.key, rejection.code, rejection.message)

changed = await space.proxies.override_many(
    {proxy_a: "dead", proxy_b: "alive"}, reason="проверка оператора", actor="ops:ivan"
)
statuses = await space.proxies.report_many({proxy_a: True, proxy_b: False}, actor="checker")
```

- `delete_many` и `override_many` возвращают `BatchResult`: `applied` — ключи применённых,
  `rejected` — отказы с кодом ошибки (`not_found`, `validation_error`, …). По умолчанию отказ
  одной строки откатывает только её; `atomic=True` отменяет всю пачку и бросает исходное
  исключение. События те же, что у одиночных команд, — на каждый прокси.
- Под `space.using(session)` пачка идёт в транзакции приложения и откатывается вместе с ней.
- `report_many` — результаты своих проверок: `True` — успех, `False` или вес — ошибка. Как и
  `report_failure`, это факты внешнего мира: каждый отчёт фиксируется своей транзакцией и не
  откатывается с транзакцией приложения. Неизвестный прокси в ответ не попадает.

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
    dead --> alive : 2 успешные проверки подряд / override
```

- Ошибки прокси приходят от классификатора ошибок (`Fault(Blame.PROXY)`) или напрямую:
  `await space.proxies.report_failure(proxy_id, weight=1.0, actor="operator")`.
- Успехи уменьшают счёт: `await space.proxies.report_success(proxy_id)` — библиотека сама его
  не вызывает.
- Порог и backoff задаёт `IdentityHub(proxy_health=ProxyHealthPolicy(error_threshold=3.0,
  base_backoff=timedelta(seconds=60), max_backoff=timedelta(hours=1), dead_after=5))`.
  К паузам добавляется случайная доля до `jitter` (20 %), чтобы прокси одной группы не
  проверялись залпом.
- `dead` — не навсегда: раз в `max_backoff` такой прокси снова попадает в `recheck_due` и
  возвращается в работу после двух успешных проверок.
- Один вызов чекера ограничен `check_timeout` (15 с); превышение — отказ прокси.
- **Сбой чекера — не отказ прокси.** Если чекер бросил исключение или в одной группе разом
  отказали все проверенные прокси (два и больше), библиотека считает это недоступностью
  контрольного адреса или провайдера: счётчик неудач не растёт, прокси не уходят в `dead`,
  перепроверка откладывается, пишется событие `proxy.checker.failed` (`pool`, `reason`:
  `checker_error` или `all_failed`, число прокси). Полчаса простоя провайдера больше не
  выводят из строя всю группу.
- Закрепление (`override(..., pin_for=...)`) защищает статус от всех автоматических изменений:
  закреплённый прокси не перепроверяется по расписанию и не уходит в `dead`. Ручные `recheck`
  и `override` действуют.

**Перепроверку запускает ваш код** — в обслуживание она не входит:

```python
async def check(url: str) -> bool:
    async with httpx.AsyncClient(proxy=url, timeout=10) as c:
        return (await c.get("https://api.ipify.org")).status_code == 200

async def proxy_loop(space):
    while True:
        await space.proxies.recheck_due(check, limit=50)     # quarantined и dead, которым пора; параллельно
        await asyncio.sleep(60)

await space.proxies.recheck(proxy_id, check, actor="admin")                              # в том числе dead
await space.proxies.recheck_group("residential", check, actor="admin")                   # вся группа сразу
await space.proxies.override(proxy_id, "alive", reason="заменили", actor="admin")       # вручную
```

Обзор: `await space.admin.proxy_overview()` — статусы, загрузка, ёмкость пулов;
`await space.admin.proxy_detail(proxy_id)` — привязанные аккаунты и история.

Для своих списков — read-схема: `hub.read.proxies` (с `bound_count`) и `hub.read.proxy_bindings`
для join аккаунтов с прокси одним запросом, см. [интеграции](../integrations.md#read-схема-join-с-таблицами-приложения).

Дальше: [события и уведомления](10-events.md).
