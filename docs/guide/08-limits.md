# 8. Лимиты и реакция на ошибки

Платформы банят за слишком частые действия. Identity Hub ограничивает нагрузку на каждый
аккаунт пятью механизмами:

| Механизм | Где задаётся | Что делает | Состояние аккаунта |
|---|---|---|---|
| **Квота** | `OperationRule.quotas` в сервисе | Не больше N операций вида за окно | не меняется; `operation()` бросает `QuotaExhausted` |
| **Троттлинг** | `Service.throttles` | Равномерно ограничивает частоту вызовов по скользящему окну | не меняется; `lease.throttle()` ждёт слот |
| **Pacing** | `space.pools.define(..., pacing=)` | После N операций — отдых на случайное время | `availability = paused` |
| **Cooldown** | `op.cool_down()` или `Fault(retry_after=)` | Пауза по требованию платформы (flood-wait) | `availability = cooling` |
| **Карантин** | `Service.quarantine_threshold` | Слишком много ошибок по вине аккаунта | `availability = quarantined` |

Все лимиты считаются **на аккаунт**. Квот на пул, прокси или сервис целиком нет.

## Троттлинг частоты

```python
from datetime import timedelta
from identity_hub import Service, Throttle

TELEGRAM = Service(
    ...,
    throttles={"api": Throttle(30, timedelta(minutes=1))},
)

await lease.throttle("api")                 # дождаться и занять слот
retry_after = await lease.throttle("api", wait=False)  # timedelta; будущий слот не занят
```

Троттлинг использует GCRA. При `burst=1` в любом окне `per` допускается не больше `limit`
вызовов; `burst=N` добавляет до `N − 1` вызовов краткого всплеска. Для эксклюзивной аренды счёт
идёт в памяти держателя без запроса к БД на каждый вызов. TAT читается при захвате и монотонно
сохраняется при освобождении, поэтому лимит продолжается после смены воркера. Неизвестное имя —
`ValidationFailed`; потеря аренды во время ожидания — `LeaseLost`.

## Операция

Каждое действие на платформе оборачивайте в `lease.operation`:

```python
async with lease.operation("send_message", key=f"send:{job.id}") as op:
    await client.send(...)
```

```mermaid
flowchart TD
    S(["lease.operation(kind, key)"]) --> F{"аренда жива,<br/>fencing совпадает?"}
    F -- нет --> E1["LeaseLost / StaleFencing"]
    F -- да --> A{"состояние допускает<br/>операцию?"}
    A -- нет --> E2["OperationNotAllowed(blockers)"]
    A -- да --> K{"key уже использован<br/>и прошлая попытка не упала?"}
    K -- да --> D["op.duplicate = True<br/>квота не списывается"]
    K -- "нет или упала" --> Q{"квоты хватает?<br/>(атомарно, все окна)"}
    Q -- нет --> E3["QuotaExhausted(retry_at)<br/>тело не выполняется"]
    Q -- да --> P["списать квоту и pacing"]
    D --> B["тело блока: вызов платформы"]
    P --> B
    B -- успех --> OK["error_score × 0.5"]
    B -- исключение --> CL["ErrorClassifier → последствия<br/>исключение пробрасывается"]
```

- Квота списывается **до** вызова платформы и в отдельной транзакции: откат вашей транзакции
  её не вернёт. Так гарантируется, что лимит не будет превышен даже при сбоях.
- **Ключ `key`** делает операцию идемпотентной: повтор задачи с тем же ключом не выполнит
  действие и не спишет квоту второй раз. Подробности — в разделе
  [«Идемпотентность операции»](#идемпотентность-операции).
- Исключение внутри блока не глушится — оно пробрасывается, а аренда закрывается как `failed`,
  если вы его не поймали.

## Идемпотентность операции

Ключ `key` — имя логического действия («отправить сообщение по задаче 42»). Библиотека помнит
исход прошлой попытки с этим ключом и сообщает его через `op.duplicate` и `op.previous_outcome`:

| Прошлая попытка | `op.duplicate` | `op.previous_outcome` | Квота | Что делать |
|---|---|---|---|---|
| не было | `False` | `None` | списана | выполнить действие |
| упала с исключением | `False` | `"failed"` | списана снова | выполнить действие — это новая попытка |
| завершилась успешно | `True` | `"succeeded"` | не списана | пропустить: действие уже выполнено |
| исход неизвестен | `True` | `None` | не списана | сверить с платформой, прежде чем повторять |

«Исход неизвестен» — прошлая попытка ещё выполняется в другом воркере, была отменена
(`CancelledError`, потеря аренды) или процесс упал до записи результата. Действие могло
выполниться, поэтому библиотека не повторяет его сама.

```python
async with lease.operation("send_message", key=f"send:{job.id}") as op:
    if not op.duplicate:
        await client.send(job.chat, job.text)
    elif op.previous_outcome is None and not await client.was_sent(job.id):
        raise RetryLater  # исход прошлой попытки неизвестен, на платформе действия нет
```

**Тело блока выполняется всегда** — пропускает действие ваш код по `op.duplicate`.

- Ключ действует **в пределах пространства**: одинаковый ключ в двух пространствах — две
  независимые операции.
- Аккаунт в ключ не входит: его выбирает пул, и повтор задачи под другим аккаунтом остаётся
  повтором той же операции.
- Тот же ключ с **другим видом операции** — ошибка `IdempotencyConflict`: тело не выполняется,
  квота не списывается. Включайте в ключ id задачи, а не только её тип.
- `QuotaExhausted` ключ не занимает: повтор после пополнения квоты пройдёт как первая попытка.
- Запись ключа живёт `Retention.operation_keys` (по умолчанию 7 дней) с момента последней
  попытки; после этого ключ свободен.
- Если действие выполнено, а записать его результат не удалось (сбой БД сразу после вызова
  платформы), исключение наружу не выходит: в лог пишется предупреждение, в метрику —
  `identity_hub_failures_total{scope="operation_bookkeeping"}`. Повтор такой задачи получит
  «исход неизвестен».

## Квоты

```python
from datetime import datetime, timedelta, timezone
from identity_hub import OperationRule, Quota

MSK = timezone(timedelta(hours=3))

operations = {
    "send_message": OperationRule(quotas=(
        Quota(20, timedelta(hours=1)),                                        # 20 в час
        Quota(100, timedelta(days=1), origin=datetime(2000, 1, 3, tzinfo=MSK)),  # 100 в сутки, сутки с полуночи МСК
    )),
}
```

- **Окна фиксированные**, не скользящие: час — с начала часа, сутки — с полуночи по `origin`
  (по умолчанию UTC), неделя — с понедельника.
- Все окна операции списываются **атомарно**: если одно исчерпано, остальные не тронуты.
- `QuotaExhausted.retry_at` — когда откроется окно. Сбросить квоту вручную нельзя.
- При расходе последней единицы пишется событие `identity.quota.exhausted`.
- Текущий расход: `(await space.identities.card(id)).quotas`.

**Квоты при выборе аккаунта.** По умолчанию `pool.lease(for_operation=...)` квоты не смотрит и
может выдать аккаунт с исчерпанным окном. `require_quota=True` отсекает такие аккаунты на этапе
выборки:

```python
try:
    async with pool.lease(for_operation="send_message", require_quota=True, wait=10) as lease:
        async with lease.operation("send_message", key=key):
            await send(...)
except LeaseUnavailable as exc:
    if exc.reason == "quota":            # свободные аккаунты есть, но квота исчерпана у всех
        await asyncio.sleep(exc.retry_after)   # секунды до ближайшего открытия окна
```

- Учитываются все окна квот операции; pacing в фильтр не входит.
- `require_quota` без `for_operation` — `ValidationFailed`.
- Если подходящие аккаунты заняты, причина — `busy`, а не `quota`: `retry_after` заполняется только
  для квоты.
- Фильтр — оценка на момент выборки, а не резерв. В эксклюзивном режиме этого достаточно: аккаунт
  держит один воркер. В `SHARED` параллельные держатели могут исчерпать окно уже после выдачи,
  поэтому `QuotaExhausted` из `lease.operation()` обрабатывать всё равно нужно.
- **Цена.** Порядок выборки отделён от показываемого пользователю `last_leased_at`. Если в голове
  LRU оказывается аккаунт с исчерпанной квотой, выборка переносит ограниченную пачку таких
  аккаунтов в хвост; следующие захваты снова делают один точечный lookup квоты. Поэтому время
  установившейся выборки не растёт с числом исчерпанных аккаунтов. На 100 000 аккаунтов с 50 000
  исчерпанных p95 после прогрева остаётся в пределах 25% от базовой аренды в том же окружении.

## Pacing — имитация живого человека

Живые пользователи не работают без перерыва. Pacing отправляет аккаунт отдыхать после серии
операций:

```python
from identity_hub import Pacing, Quota

senders = space.pools.define(
    "senders", service=TELEGRAM,
    pacing=Pacing(work=Quota(15, timedelta(hours=1)), pause=(20, 45)),   # после 15 операций — 20–45 минут отдыха
)

async with senders.lease() as lease:
    async with lease.operation("send_message") as op:
        await send(...)
    if op.paused_until:
        log.info("аккаунт ушёл отдыхать до %s", op.paused_until)
```

- Pacing — свойство **пула**: одни и те же аккаунты могут работать размеренно в пуле рассылки
  и без пауз в пуле парсинга.
- Счётчик общий для всех видов операций аккаунта в этом пуле.
- Операция, исчерпавшая серию, выполняется; отдыхает аккаунт после неё (`availability=paused`),
  затем возвращается сам.

## Cooldown — flood-wait от платформы

```python
async with lease.operation("send_message") as op:
    try:
        await client.send(...)
    except FloodWaitError as e:
        await op.cool_down(e.seconds)        # availability = cooling на e.seconds
        raise
```

Срок только удлиняется: повторный `cool_down` с меньшим сроком не сократит паузу. То же
делается автоматически через классификатор ошибок (`Fault(retry_after=...)`).

## Классификатор ошибок

Классификатор — главный способ сообщить библиотеке, что случилось на платформе. Он получает
исключение из тела операции и возвращает `Fault` — чья это вина:

```python
from datetime import timedelta
from identity_hub import Blame, Fault, Standing


class TgErrors:
    def classify(self, exc):
        match exc:
            case FloodWaitError():
                return Fault(Blame.IDENTITY, retry_after=timedelta(seconds=exc.seconds), code="FLOOD_WAIT")
            case AuthKeyUnregisteredError():
                return Fault(Blame.SESSION_INVALID, code="AUTH_KEY_UNREGISTERED")
            case UserDeactivatedBanError():
                return Fault(Blame.IDENTITY_BANNED, code="USER_DEACTIVATED_BAN")
            case PeerFloodError():
                return Fault(Blame.IDENTITY_BANNED, standing=Standing.LIMITED, code="PEER_FLOOD")
            case ProxyConnectionError():
                return Fault(Blame.PROXY, weight=1.0)
            case _:
                return None                     # ошибка запроса, аккаунт не виноват


TELEGRAM = Service(..., error_classifier=TgErrors())
```

| `Blame` | Смысл | Последствие |
|---|---|---|
| `IDENTITY` | Аккаунт перегружен или ведёт себя подозрительно | `error_score += weight`; с `retry_after` → `cooling`; при `error_score ≥ quarantine_threshold` → `quarantined` |
| `SESSION_INVALID` | Сессия больше не работает | сессия помечается невалидной, `auth → session_invalid` |
| `IDENTITY_BANNED` | Платформа ограничила аккаунт | `standing → fault.standing` (по умолчанию `banned`) |
| `PROXY` | Виноват прокси | `error_score` прокси растёт, см. [прокси](09-proxies.md) |
| `REQUEST` или `None` | Ошибка запроса, данных, сети | состояние не меняется |

`Fault(blame, retry_after=None, standing=None, code=None, raw_status=None, weight=1.0)`.
`code` и `raw_status` уходят в события: не кладите в них `str(exc)` — текст исключения часто
содержит URL с ключом. Библиотека обрезает и очищает эти поля, но короткий код платформы
(`FLOOD_WAIT`, `AUTH_KEY_UNREGISTERED`) полезнее.

**Карантин.** `error_score` растёт на ошибках `IDENTITY` и уменьшается вдвое при каждой успешной
операции. Если он достиг `Service.quarantine_threshold` (по умолчанию 5), аккаунт уходит в
`quarantined` и не выдаётся, пока администратор не снимет карантин override-ом. Если ваш код
падал из-за собственной ошибки, а не аккаунта, — вернуть аккаунт можно так:

```python
await space.identities.override(identity_id, Override("availability", "", "available", reason="баг в воркере", actor="admin"))
```

Дальше: [прокси](09-proxies.md).
