# Identity Hub: внешняя read-модель состояния аккаунтов

Статус: verified — переписан после сверки каждой посылки с кодом `src/identity_hub/`
Целевая аудитория: разработчики и сопровождающие Identity Hub, интеграторы `server-admins`
Тип документа: архитектурный issue
Предыдущая редакция: предлагала новый `Projection API` (`begin_snapshot`/`scan`/`subscribe_batch`
с полным state vector). Проверка показала, что большая часть предложенного уже реализована,
а подтверждённые пробелы находятся в другом месте.

## Итог проверки

| Посылка прошлой редакции | Вердикт | Почему |
|---|---|---|
| Приложение вынуждено собирать состояние из нескольких регионов | **неверно** | state vector денормализован в строке аккаунта |
| Контракт раскроет физическую схему, нужен доменный слой | **неверно** | доменный слой есть: версионированная read-схема `hub.read` |
| Нужно добавить Transactional Outbox и change feed | **неверно** | реализованы, включая горизонт видимости и курсор на потребителя |
| Нужен `begin_snapshot()` для согласованности snapshot и потока | **не нужно** | достаточно «подписка первой, snapshot вторым» + `revision`-guard |
| Нужно передавать полное состояние внутри события | **не нужно** | дорого; change notification + батчевый lookup даёт тот же результат |
| Пропуск события после `max_attempts` ломает проекцию | **верно** | подтверждённый пробел, см. `SP-1` |
| Истёкший курсор должен давать явную ошибку | **верно** | подтверждённый пробел, см. `SP-2` |
| Проекции нужен `verification` | **верно, частично** | tier считается в Python, в SQL недоступен, см. `SP-3` |
| Результат импорта должен нести ревизию | **верно, мелко** | см. `SP-4` |

## Что уже есть в библиотеке

**Денормализованный state vector.** `src/identity_hub/postgres/models.py` — `lifecycle`,
`standing`, `standing_code`, `standing_until`, `auth_state`, `availability`,
`availability_until`, `requirements`, `verified_factors`, `revision`, `updated_at` лежат в одной
строке `ih_identities`. Join таблицы регионов для итогового состояния не нужен.

**Публичная версионированная read-схема.** `src/identity_hub/postgres/read_schema.py:1` —
`hub.read`: отдельные `Table` в собственном `MetaData` с белым списком колонок поверх физических
`ih_*`, `READ_SCHEMA_VERSION = 4`, явная цель — «стабильные selectable для join с таблицами
приложения и для `pool.lease(among=select(...))`». Секретных и служебных колонок в схеме нет.
Это и есть стабильный доменный контракт, который прошлая редакция предлагала спроектировать
заново: `IdentityStateRow` — это `select` из `hub.read.identities`.

**Transactional Outbox.** `src/identity_hub/postgres/models.py:1130`, докстринг модели:
«Журнал событий = transactional outbox. Пишется в той же транзакции, что и изменение.
Потребители читают по курсору `(tx_id, id)` и только ниже горизонта видимости
`pg_snapshot_xmin`, чтобы не пропустить события транзакций, закоммиченных позже с меньшим id».
Есть частичный индекс `ix_ih_events_cursor (tx_id, id) WHERE external`.

**Курсор на потребителя.** `ih_event_cursors` (`consumer`, `last_tx_id`, `last_id`), один
активный экземпляр на потребителя через `FOR UPDATE SKIP LOCKED`.

**Ревизия на событии.** `revision` в `ih_events` (`D-205`): ревизия аккаунта после команды,
все события одной команды несут одну ревизию. Это готовый guard идемпотентного потребителя.

**Пачки и метрики.** `subscribe_batch` отдаёт до 100 событий в порядке журнала
(`src/identity_hub/dispatch/dispatcher.py:163`), метрика `identity_hub_dispatcher_cursor_lag`,
`start="now"` для подписчика с текущего момента.

Иными словами, «Debezium на доменном уровне» уже построен. Строить его второй раз не нужно.

## Почему `begin_snapshot()` не нужен

Согласованность snapshot и потока достигается порядком операций, а не новым API:

1. Оформить подписку с `start="now"`. Курсор потребителя встаёт на текущий горизонт журнала.
2. Прочитать snapshot страницами keyset-пагинации по `hub.read.identities`.
3. Применять события из потока с проверкой `revision`.

Поток заведомо начинается **не позже** snapshot, поэтому пропустить изменение нельзя. Строка из
snapshot может оказаться новее события — тогда `revision`-guard отбросит событие. Повтор
доставки безопасен по той же причине.

Debezium решает ту же задачу watermark-ами (incremental snapshot, алгоритм DBLog) только
потому, что в строке Postgres нет монотонной прикладной ревизии. В Identity Hub она есть
(`ih_identities.revision`), и watermark-и не требуются.

## Почему полное состояние не нужно передавать в событии

Прошлая редакция требовала `IdentityStateChange.state` — полный вектор внутри события
(«event-carried state transfer»). Это дороже и не даёт выигрыша:

- хранение раздувается: вектор дублируется в каждом событии аккаунта за весь retention;
- read-модели нужно **актуальное** состояние, а не состояние на момент события; при
  at-least-once и `revision`-guard проекция всё равно сходится к последней ревизии;
- потребитель получает то же самое одним запросом на пачку:
  `SELECT ... FROM hub.read.identities WHERE id = ANY(:ids)`.

Этот приём — change notification плюс батчевый lookup — уже описан в `docs/guide/10-events.md`
как рекомендуемый способ использовать `subscribe_batch`. Работы в библиотеке он не требует.

## Подтверждённые пробелы

### SP-1. Пропуск события рушит проекцию молча

`src/identity_hub/dispatch/dispatcher.py:109` и `:163`: после `max_attempts` неудач подряд
(по умолчанию 5) событие **пропускается**, подписчик получает следующие. Пропуск виден
(`dispatcher.event.skipped`, лог `ERROR`, счётчик
`identity_hub_failures_total{scope="subscriber", reason="skipped"}`), но проекция после пропуска
расходится с источником **навсегда**: следующее событие того же аккаунта несёт большую ревизию,
и guard уже не вернёт потерянное изменение.

Счётчик попыток живёт в памяти процесса, поэтому перезапуск диспетчера отсчёт обнуляет — для
проекции это случайная величина, а не политика.

Нужна политика отказа, при которой курсор остаётся перед сбойным событием неограниченно долго.

### SP-2. Очистка журнала не смотрит на курсоры потребителей

`src/identity_hub/postgres/store.py:6234` (`cleanup_maintenance`): события удаляются только по
возрасту — `older(EVENTS.c.occurred_at, "events")`, по умолчанию 180 дней (`D-004`).
Минимальный `(last_tx_id, last_id)` из `ih_event_cursors` не учитывается.

Следствие: потребитель, отставший больше чем на retention (длительная остановка, застрявшая
пачка из `SP-1`), теряет события без ошибки и без метрики потери. Проекция расходится, и узнать
об этом можно только сверкой.

Нужно и то, и другое: не удалять события ниже минимального курсора активных потребителей и
давать явную диагностику, когда курсор всё-таки оказался вне журнала.

### SP-3. `verification` недоступен в SQL

Единственный элемент вектора, которого нет в read-схеме. Tier выводится в Python из
`verified_factors` и конфигурации сервиса: `src/identity_hub/read/search.py:369`
(`_highest_tier(self.hub.service(row.service), frozenset(row.verified_factors))`). Потребитель,
который фильтрует и строит фасеты в SQL, воспроизвести его не может, не скопировав
конфигурацию сервисов.

### SP-4. Результат импорта не несёт ревизию

`src/identity_hub/importing.py:89` (`ImportReport`): `created` и `updated` — списки `UUID`.
Чтобы создать локальную строку сразу с правильной ревизией, потребитель делает дополнительный
запрос. Полный `ImportedIdentity.state` из прошлой редакции избыточен — достаточно ревизии.

### SP-5. Нет документированного рецепта

Отдельные части (`hub.read`, `subscribe_batch`, `revision`, `start="now"`) описаны в разных
разделах руководства. Сборка «внешняя read-модель состояния» как связный рецепт — с порядком
операций, keyset-пагинацией, `revision`-guard и периодической сверкой — не описана, поэтому
каждый интегратор приходит к предложению построить `Projection API`.

## Рекомендуемый контракт

Библиотека закрывает `SP-1`…`SP-5`. Snapshot, пагинация, UPSERT и сверка остаются на стороне
потребителя: это примерно 60 строк в `server-admins`.

```python
# 1. Подписка — ДО snapshot. Курсор встаёт на текущий горизонт журнала.
hub.events.subscribe_batch(
    "server-admins-state",
    apply_changes,
    types={"identity.*"},
    space=space,
    start="now",
    failure_policy="block",      # SP-1
)

# 2. Snapshot: keyset по первичному ключу, один запрос на страницу, только hub.read.
i = hub.read.identities
page = await conn.execute(
    select(
        i.c.id, i.c.service, i.c.lifecycle, i.c.standing, i.c.auth,
        i.c.availability, i.c.verified_factors, i.c.verification_tier_rank,  # SP-3
        i.c.revision, i.c.updated_at,
    )
    .where(i.c.space_id == space_id, i.c.id > after)
    .order_by(i.c.id)
    .limit(5000)
)

# 3. Change feed: один lookup на пачку плюс set-based UPSERT с revision-guard.
async def apply_changes(events: list[Event]) -> None:
    ids = {e.identity_id for e in events if e.identity_id is not None}
    states = await load_states(ids)          # один SELECT ... WHERE id = ANY(...)
    await upsert_projection(states)          # ON CONFLICT ... WHERE excluded.revision > revision
```

Guard на стороне потребителя:

```sql
UPDATE account_projection
   SET state = :state, identity_revision = :revision
 WHERE identity_id = :identity_id
   AND identity_revision < :revision;
```

Tombstone отдельного механизма не требует: событие `identity.deleted` уже есть, строка в
журнале переживает удаление аккаунта (`identity_id` в `ih_events` без FK).

Периодическая сверка — тот же запрос шага 2 без подписки: расхождения исправляются пачками.

## Что сознательно не делается

- Отдельная подсистема `space.identity_states` со snapshot-объектами, `scan()`, tombstone-API,
  dead-letter-таблицей и rebuild API. Snapshot — это `select` потребителя, rebuild — удаление
  строки курсора потребителя.
- Полное состояние внутри события (см. раздел выше).
- Бенчмарки на 100 тысячах и 1 миллионе identity как условие приёмки. Keyset-скан по
  первичному ключу имеет известную стоимость; отдельный бенчмарк здесь ничего не решает.
  Что действительно стоит проверить — покрывающий индекс под `WHERE space_id = ? ORDER BY id`.
- Row-level и statement-level триггеры, materialized view, Debezium, Kafka, полный Event
  Sourcing. Прошлая редакция отклоняет их верно, выводы сохранены ниже.

## Альтернативы

| Вариант | Преимущества | Недостатки | Решение |
| --- | --- | --- | --- |
| Row-level триггеры | Синхронная консистентность | Замедляют запись, связывают схемы | Не использовать |
| Statement-level триггер | Один set-based вызов на statement | Связывает схемы, не работает между БД | Не использовать |
| PostgreSQL materialized view | Простая модель чтения | Дорогой refresh, ограниченная свежесть | Не использовать для оперативного списка |
| Обычный `VIEW` поверх `hub.read` | Нулевая стоимость синхронизации | Только в одной базе, не ускоряет фильтры приложения | Годится, пока нет требований к задержке |
| Debezium CDC | Готовые snapshot и streaming | Kafka/Connect, replication slot, контракт физической схемы | При нескольких внешних системах или готовой CDC-инфраструктуре |
| `hub.read` + `subscribe_batch` + `revision` | Контракт уже есть, схемы не связаны | Eventual consistency, нужен consumer | **Рекомендуемый вариант** |

## Критерии готовности

- [x] `failure_policy="block"` никогда не пропускает событие: курсор стоит перед сбойным до успеха.
- [x] Застрявший потребитель виден в метриках и в read-модели состояния диспетчера.
- [x] Очистка журнала не удаляет события ниже минимального курсора активных потребителей.
- [x] Курсор вне горизонта журнала даёт явную ошибку и документированный rebuild.
- [x] `verification_tier_rank` доступен в `hub.read.identities` и совпадает со значением
      read-модели админки на том же наборе данных.
- [x] `ImportReport` возвращает ревизию созданных и обновлённых аккаунтов.
- [x] Интеграционный тест: изменение во время snapshot не теряется при порядке
      «подписка → snapshot → поток».
- [x] Интеграционный тест: потребитель восстанавливается после остановки без расхождения.
- [x] Интеграционный тест: повторная и частично повторная доставка пачки не портит проекцию.
- [x] Раздел руководства описывает рецепт, консистентность, повторы, retention и удаление.

## План внедрения

Очередь `SP*`: `docs/state-projection-plan.md`, прогресс — `docs/state-projection-progress.md`,
решение — `D-325`.

## Источники

- [CQRS Pattern, Microsoft](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs)
- [Materialized View Pattern, Microsoft](https://learn.microsoft.com/en-us/azure/architecture/patterns/materialized-view)
- [Transactional Outbox, microservices.io](https://microservices.io/patterns/data/transactional-outbox.html)
- [Debezium PostgreSQL Connector](https://debezium.io/documentation/reference/stable/connectors/postgresql.html)
- [Debezium Incremental Snapshots](https://debezium.io/documentation/reference/stable/connectors/postgresql.html#postgresql-incremental-snapshots)
- [DBLog: A Watermark Based Change-Data-Capture Framework](https://arxiv.org/abs/2010.12597)
- [PostgreSQL Logical Decoding Concepts](https://www.postgresql.org/docs/current/logicaldecoding-explanation.html)
- [PostgreSQL REFRESH MATERIALIZED VIEW](https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html)
