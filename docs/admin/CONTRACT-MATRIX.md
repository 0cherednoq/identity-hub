# Матрица контрактов Identity Hub Admin

Статус: проверено по рабочему дереву 2026-09-27.  
Основной читатель: исполнитель задач `A*`.  
Тип документа: справочник фактических контрактов и пробелов реализации.

Матрица связывает все 14 страниц из [`ADMIN-PAGES.md`](ADMIN-PAGES.md) с публичными API
библиотеки, безопасными DTO, событиями, таблицами и задачами из [`PLAN.md`](PLAN.md). Админка не
обращается к таблицам Identity Hub напрямую. Упоминание таблицы ниже указывает владельца факта,
который должна инкапсулировать read-модель библиотеки.

## Как читать статусы

| Статус | Значение |
|---|---|
| `готово` | Публичный безопасный контракт библиотеки уже существует и покрывает поле или действие |
| `расширить read` | Факт хранится, но нет полной публичной read-модели для страницы |
| `добавить команду` | Нужной публичной доменной команды нет или ей не хватает concurrency/audit-контракта |
| `добавить схему` | Факт нельзя надёжно получить из текущей схемы или событий |
| `производное` | Read-модель должна вычислить поле из существующих фактов внутри библиотеки |
| `API/UI` | Библиотечный контракт достаточен, остаются HTTP API и интерфейс админки |

В матрице нет статуса «неизвестно». Каждый пробел назначен задаче `A*`. Поля секретов намеренно
не перечислены. Для них указываются только наличие, безопасные метаданные и отдельное действие
раскрытия по [A-D017](DECISIONS.md).

## Текущая поверхность библиотеки

| Область | Уже доступно | Ограничение для админки |
|---|---|---|
| Поиск | `SpaceHandle.search()`, `SearchPage`, `IdentityRow` | Только базовая строка запроса, один порядок `(created_at, id)`, нет большинства фильтров §3.4 |
| Карточка | `identity_card()`, `IdentityCard`, `RegionInfo`, `LeaseInfo`, `ProxyInfo` | Нет полного revision/concurrency-контракта, безопасных metadata credentials/sessions и части вкладок |
| Аренды | `lease_history()`, `lease_stats()`, `revoke_lease()` | Статистика только одного аккаунта; нет окон пространства, live-list и группировок |
| Состояние | `set_lifecycle()`, `retire()`, `override()`, `resync()`, `reconcile()` | Командам нужен единый `expected_revision`, actor/reason и idempotency gateway |
| Верификация | `verification_status()`, объект `Verification`, requirements-команды | Нет общей операторской read-модели и списка доступных действий |
| Прокси | `get_proxy()`, health/recheck/override и импорт | Существующий DTO содержит внутренний payload и не подходит для HTTP; нет безопасного overview/rebind-контракта |
| Связи | `links_of()`, `link_summary()`, `impact_of()`, `link()`, `unlink()` | Нужны page DTO и concurrency/audit gateway |
| Импорт | `import_many()`, `import_files()`, `ImportReport` | Нет долговечного `run_id`, прогресса и отчёта запуска после рестарта |
| События | каталог `CATALOG`, `Event`, dispatcher | Нет публичного фильтруемого журнала для чтения |
| Вебхуки | create, rotate, enable/disable, retry, delivery list | Нет безопасного списка endpoint-ов и общей страницы очереди |
| Уведомления | CRUD каналов/правил, dispatcher | Нет списка отправок, preview и test-send |
| Сервисы | `IdentityHub.services`, `Service` | Нужен стабильный безопасный DTO вместо сериализации runtime-объекта |
| Фоновые работы | `Dispatcher.run_once()/drain()/run()` | Нет status/heartbeat/read API; maintenance ещё выполняется задачей `P9.1` |
| Секреты | `update_credentials()`, `reveal_secret()` с событием аудита | Раскрытие требует дополнительной re-auth и HTTP no-store защиты в `A3.5` |

Опорные исходники: [`read/search.py`](../../src/identity_hub/read/search.py),
[`read/cards.py`](../../src/identity_hub/read/cards.py),
[`store/base.py`](../../src/identity_hub/store/base.py),
[`events/catalog.py`](../../src/identity_hub/events/catalog.py) и
[`postgres/models.py`](../../src/identity_hub/postgres/models.py).

## 1. Обзор

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Доступные пространства | `ih_spaces`; публичного list-контракта нет | `расширить read`: `list_spaces`, A2.1; доступ ограничивает A1.2/A1.3 |
| Всего, пригодно, тяжёлые состояния, открытые challenge | identity/region/challenge facts; частично `pool_summary()` | `расширить read` и `производное`: `space_overview`, A2.1 |
| Текущие аренды | lease facts; `identity_card()` умеет только одну identity | `расширить read`: A2.1/A2.4 |
| Тревоги ёмкости, прокси, endpoint, drift | события `pool.capacity.*`, `proxy.pool.exhausted`, `webhook.endpoint.disabled`, `identity.state.drift` | `расширить read`: оперативная часть A2.1, историческая оценка A4.7 |
| Всплеск банов, dispatcher lag | standing event rollups и cursor facts | `готово` для ряда банов; evaluator A4.7 |
| Аренды за 15м/1ч/6ч/24ч | lease history | `расширить read`: A2.4, подключение на обзор A4.7 |
| Метрики за 24 часа и сравнение | `ih_stats_rollup` с 1m/1h/1d buckets | `готово` как хранение; read/UI A4.5/A4.7 |
| Компактная таблица пространств | те же overview facts | `расширить read`: A2.1 |

## 2. Пространства

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Название, slug | `SpaceRef` и space facts | `расширить read`: list contract A2.1 |
| Сервисы и число аккаунтов | service/identity facts | `производное`: A2.1 |
| Всего, пригодно, в аренде | identities/leases | `производное`: A2.1 |
| Все распределения lifecycle/standing/auth/availability | identity columns и region facts; `pool_summary()` частично готов | `расширить read`: `spaces_matrix`, A2.1 |
| Challenge | challenge facts | `производное`: A2.1 |
| Добавлено и забанено за 24ч/7д | `identities.added` и `standings.entered` rollups | `готово` как ряды; подключение A4.7 |
| Матрицы standing/auth и service slice | identities | `расширить read`: A2.1; URL-переходы A1.6 |

## 3. Пространство

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Распределения регионов, пригодность, blockers | `pool_summary()`, domain admissibility | `расширить read`: безопасный `space_overview`, A2.1 |
| В аренде и свободные пригодные | lease и pool candidate facts | `производное`: `pool_capacity`, A2.1 |
| Уровни и факторы верификации | `verified_factors`, verification region facts, `Service.tiers` | `расширить read`: tier/factor counts A2.1 |
| Requirements и ближайший deadline | identity requirements и requirement region facts | `расширить read`: requirement counts A2.1 |
| Метки/теги × standing | identity labels/tags | `расширить read`: A2.1 |
| Пулы и low threshold | `Pool.count_candidates()`/`check_capacity()` и pool events | `расширить read`: read-only capacity A2.1 |
| Окна аренд | lease history | `расширить read`: A2.4 |
| Тренды 7/30 дней | event rollups без scan журнала | `готово` как хранение; A4.5/A4.7 |
| Агрегаты квот | quota usage + `Service.operations` | `расширить read`: A2.1 |

## 4. Аккаунты пространства

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| `service`, region, tag, label/profile, text, exact id | `search()` и `SearchFilter` | `готово` для базовых фильтров; HTTP/UI A2.2 |
| Пригодность, аренда, factor/tier, requirement, pin, proxy/link | факты существуют раздельно | `расширить read`: фильтры и row projection A2.2 |
| Диапазоны дат и изменение standing | identity, lease и event facts | `расширить read`: A2.2; нужный event index A4.1 |
| Безопасные колонки §3.4 | `IdentityRow` покрывает identity-поля | `расширить read`: аренда/proxy/link/quota/admissibility projection A2.2 |
| Сортировка по выбранной колонке | текущий cursor только `(created_at, id)` | `расширить read`: стабильные sort-specific cursors A2.2 |
| 50 по умолчанию, максимум 200, `1000+` | `SearchPage.total_label`; библиотека допускает максимум 500 | `API/UI`: admin cap 200 по A-D101, A2.2 |
| Массовые labels/tags/lifecycle/resync | одиночные команды уже есть | `добавить команду`: snapshot/dry-run/batch contract A3.1/A3.6 |
| Массовый cooldown и rebind proxy | частично operation/proxy internals | `добавить команду`: A3.4/A3.6 |
| CSV без секретов | безопасная row projection | `API/UI`: A3.6, аудит A3.1 |

## 5. Карточка аккаунта

| Вкладка или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Шапка, identity facts, статусы, текущая аренда, proxy | `identity_card()` | `готово` частично; дополнить blockers, heartbeat, proxy capacity и revision в A2.3 |
| Revision | `IdentityRow.event_seq` и версии region/data | `расширить read` и `добавить команду`: A-D102/D-122, A2.3/A3.1 |
| Состояние: code/until/source/actor/reason/time/sync/raw/pin | `RegionInfo` покрывает почти всё | `готово` частично; добавить pending observation и versions A2.3 |
| История переходов | catalog events | `расширить read`: A2.3 через event read A2.7 |
| Profile, attributes, labels, tags, note | `IdentityRow`; команды identities mixin | `готово`; HTTP/UI A2.3, mutations A3.1/A3.2 |
| Identity data namespaces и содержимое/version | `data_namespaces()`, `load_data()` | `расширить read`: безопасный tab contract A2.3 |
| Credentials metadata и lifecycle | текущая схема хранит один набор без list metadata | `добавить схему`: A4.1; read A2.3; actions A3.5 |
| Sessions metadata по слотам | session facts и `get_session()` внутреннего store | `расширить read`: A2.3; actions A3.3/A3.5 |
| Verification state/attempts/dates/via identity | `identity_card()` покрывает часть; verification facts содержат остальное | `расширить read`: A2.3; actions A3.3 |
| Requirements и история | identity/region facts + events | `расширить read`: A2.3; actions A3.2 |
| Challenge | `IdentityCard.open_challenge` | `готово` для чтения; ответ A3.3 |
| Login/probe operation history | долговечного operation store пока нет | `добавить схему`: A4.1; read A2.3/A2.9; actions A3.3 |
| Lease current/stats/history/logs | `lease`, `lease_stats()`, `lease_history()`; logs без публичной page model | `готово` частично; расширить A2.3/A2.4 |
| Quotas и pacing | `QuotaWindow`, service rules | `готово` частично; вычислить reset/pacing A2.3 |
| Proxy binding/history | `ProxyInfo`, `proxy.binding.changed` | `готово` частично; history A2.3/A2.6, rebind A3.4 |
| Links и impact | `Links`, `impact_of()` | `готово`; page DTO A2.3, actions A3.4 |
| Events и lease logs | event/log facts | `расширить read`: A2.7 и A2.3 |
| Reveal/update secret-bearing values | `reveal_secret()`, `update_credentials()` | `готово` на уровне библиотеки; re-auth/no-store/UI A3.5 |

## 6. Аренды

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Acquired/completed/unique/duration/end reasons/operations | lease history | `расширить read`: оконная агрегация A2.4 |
| Utilization | lease intervals + admissible count | `производное`: A2.4; историческая точность A4.4/A4.5 |
| Empty picks и wait p95 | exactly-once `telemetry.lease_empty_total` и wait `.sum/.count` rollups | `готово` как агрегаты; percentile sketch остаётся A4.5 |
| Group by pool/service/holder | lease history + identity service | `расширить read`: A2.4 |
| Live leases, heartbeat lag, expired-unclosed | lease facts | `расширить read`: A2.4 |
| Revoke | `revoke_lease()` | `готово`; gateway/reason/idempotency A3.1/A3.4 |

## 7. Аналитика

| Группа | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Intake, auth, verification, standing | именованные 1m/1h/1d event rollups | `готово`: catalog/formulas/API A4.5, графики/таблицы A4.6 |
| Tier reached/lost | атомарные events и `tiers.reached/lost` rollups | `готово`: read-модель/API A4.5 |
| Funnel, time-to, cohorts, ban lifetime/rate | events + hourly state snapshots с coverage | `готово`: bounded read/API A4.5, UI A4.6 |
| Lease/operation heatmaps, quota/cooling, utilization | bounded telemetry `.sum/.count`, lease/operation event rollups | `готово`: read/API A4.5, heatmap/UI A4.6 |
| Drift/stale sync | `state.drift`, `state.overrides` и snapshot facts | `готово`: drift series A4.5/A4.6; stale share остаётся для A4.7 |
| Proxy degradation/errors | `proxies.status` и `operations.failed{blame=proxy}` | `готово`: series + quality report A4.5 |
| Window/bucket/dimension/previous period/timezone/zero-fill | IANA timezone, 1m/1h/1d, dense zero-fill, ETag | `готово`: library/API A4.5, URL/coverage/table/export UI A4.6 |

## 8. Очередь «нужен человек»

| Раздел или действие | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Open challenge | challenge facts и `open_challenge` | `расширить read`: unified queue A2.5; answer A3.3 |
| Action required/requirements | requirements facts | `расширить read`: A2.5; resolve/resync A3.2/A3.3 |
| Manual verification | verification region/spec | `расширить read`: A2.5; approve/reject A3.3 |
| Drift after override | region raw/current/pin + `identity.state.drift` | `расширить read`: A2.5; override/unpin A3.2 |
| Quarantine | identity/proxy health facts | `расширить read`: A2.5; restore/retire A3.2/A3.4 |
| Unified deadline, urgency, reason | перечисленные facts | `производное`: A2.5 |

## 9. Прокси

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Host/scheme/pool/status/error/fail/retry/check/pin/bound/cap | proxy facts; `ProxyRow` не является безопасным web DTO | `расширить read`: `proxy_overview`, A2.6 |
| In-lease count, pool aggregates, free capacity | proxy bindings + leases | `производное`: A2.6 |
| Bound identities and status history | bindings + `proxy.status.changed` | `расширить read`: A2.6 |
| Proxy-attributed errors 24h/7d | events/operation faults | `расширить read`: A2.6; rollup A4.3/A4.5 |
| Recheck/override/import | публичные proxy methods существуют | `готово`; gateway/UI A3.4 |
| Rebind | публичной команды нет | `добавить команду`: A3.4 |

## 10. Журнал событий

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Type/group/external/severity | `CATALOG`, `EventSpec` | `готово` |
| Service/identity/proxy/lease/actor/date filters | event facts, публичного query нет | `расширить read`: A2.7 |
| Time/type/severity/links/summary and redacted detail | `Event`, `scrub()` | `расширить read`: безопасные summary/detail DTO A2.7 |
| Cursor `(occurred_at, id)` | event facts | `расширить read`: A2.7 |
| JSON export | безопасный event DTO | `API/UI`: A3.6 |

## 11. Импорт

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Service/on_existing/records/links/proxy/session verification | `ImportRecord`, `import_many()/import_files()` | `готово` для синхронного library flow |
| Run id, actor/source/timestamps/progress/counts/report | `stage_import()`, `ImportRunner`, `import_runs()/import_run()` и `ih_import_runs` | `готово`: durable claim/fencing, polling UI и cancel boundary A3.7/A4.1 |
| Per-record redacted failure and created identity links | `ImportRunInfo.report` содержит только created IDs и ограниченные safe reason/code | `готово`: отчёт и account links A3.7, секреты не возвращаются |
| CSV/JSONL upload limits and no source retention | строгий admin parser; encrypted `ih_import_items` удаляется в terminal state | `готово`: 10 МиБ, 10 000 записей, 5 секунд, depth/field limits; A-D110/D-134 |

## 12. Уведомления и вебхуки

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Telegram channel safe metadata | `Notifications.channels()`/`NotificationChannel` | `готово`; API/UI A2.10 |
| Rules safe metadata | `Notifications.rules()`/`NotificationRule` | `готово`; API/UI A2.10 |
| Notification send journal | notification facts без публичного list API | `расширить read`: A2.10 |
| Webhook endpoint safe metadata | endpoint facts; публичного list API нет | `расширить read`: A2.10 |
| Delivery journal | `Webhooks.deliveries()` | `готово` частично; cursor/filter A2.10 |
| Preview count | event matching exists, query отсутствует | `расширить read`: A2.10 |
| CRUD/rotate/enable/retry | публичные channel/rule/webhook команды и scoped delivery retry | `готово`: validation/gateway/RBAC/audit/UI A3.8, D-132 |
| Test-send and manual notification retry | прямые transport probes и scoped retry без cursor mutation | `готово`: A3.8, D-132 |

## 13. Сервисы

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Operations, quotas, rules, standing codes | `Service` runtime registry | `готово` как Python API; безопасный DTO A2.8 |
| Verifications, dependencies, tiers | `Service.verifications/tiers` | `готово`; DTO/UI A2.8 |
| Authenticator/probes/lease/proxy/search capabilities | `Service` configuration | `производное`: capability DTO A2.8 |
| Counts by accessible space | identity facts | `расширить read`: A2.1/A2.8 |

## 14. Состояние фоновых работ

| Блок или поле | Владелец факта сейчас | Классификация и задача |
|---|---|---|
| Consumer cursor, event/seconds lag, last run | event cursor/event facts; публичного status API нет | `расширить read`: A2.9 |
| Webhook/notification pending/retry/dead | delivery facts | `расширить read`: A2.9 |
| Maintenance last run/processed | `P9.1` ещё `in_progress` на момент аудита | `расширить read`: после P9.1, A2.9 |
| Provider operation states/stale claims | постоянного operation fact нет | `добавить схему`: A4.1; read A2.9 |
| Stale probes by region | current sync facts | `производное`: A2.9 |
| Health thresholds and redacted last errors | policy + queue facts | `производное`: A2.9 |

## Матрица событий

| Назначение | События, которые уже есть | Пробел |
|---|---|---|
| Регистрация и импорт | `identity.registered`, `identity.imported` | Import event должен ссылаться на run A4.1 |
| Auth/login/challenge | `identity.auth.changed`, `identity.login.*`, `identity.challenge.*` | Нет долговечной истории provider operations A4.1 |
| Verification | events и `verifications.*`/`tiers.*` rollups | `готово`; публичные формулы A4.5 |
| Standing/lifecycle/availability | events для переходов, `ih_state_snapshots` для historical counts | `готово`; read-модель A4.5 |
| Manual state and drift | `identity.state.overridden`, `identity.state.drift` | Достаточно после read projection A2.5/A2.7 |
| Lease/operation | internal event rollups и exactly-once buffered telemetry rollups | `готово` как агрегаты; публичные формулы A4.5 |
| Proxy | `proxy.status.changed`, `proxy.binding.changed`, `proxy.pool.exhausted` | Attribution series требует rollup A4.3/A4.5 |
| Capacity/delivery | `pool.capacity.*`, `worker.leases_lost`, `webhook.endpoint.disabled`, cursor-lag и delivery-queue gauges | `готово` для оперативной телеметрии; исторические ряды A4.3/A4.5 |
| Secrets | `identity.credentials.updated`, `identity.secret.revealed` | Новые secret kinds должны использовать тот же audit family A3.5 |

## Матрица изменений схемы

| Изменение | Почему текущих фактов недостаточно | Задача |
|---|---|---|
| `space.timezone` | Нельзя воспроизводимо определить дневные границы пространства | A4.1 |
| Import runs + redacted item report | `ImportRunInfo` + encrypted resumable staging; terminal report живёт 90 дней | готово в A4.1/A3.7 |
| Provider operations | Нужны durable claim/retry/history для login и probes | A4.1 |
| Credentials metadata/list lifecycle | Текущий single-payload контракт не представляет список §3.5 | A4.1/A2.3/A3.5 |
| Time rollups | `ih_stats_rollup`; 30-дневный запрос не обращается к `ih_events` | готово в A4.3 |
| State snapshots | hourly counts + отдельный complete coverage marker, включая пустые пространства | готово в A4.4 |
| Lease/event indexes из §5 | Нужны оконные, holder и identity/type запросы | A4.1, после проверки query plans A5.2 |

## Проверенные расхождения документов

| Расхождение | Решение |
|---|---|
| `ADMIN-PAGES.md` требовал брать пространство только из сессии, а A-D005 задаёт его в URL | URL содержит `space_slug`; A1.3 всегда проверяет его против `allowed_spaces`. Исправлено в `ADMIN-PAGES.md` |
| `ADMIN-STACK.md` называл React Router, тогда как стек фиксирует TanStack Router | Используется TanStack Router. Исправлено в `ADMIN-STACK.md` |
| `ADMIN-PAGES.md` допускал SQL Core read-схему для админки, а A-D003 запрещает прямой SQL | SQL остаётся внутри библиотеки; admin backend вызывает типизированные read-модели. Исправлено в `ADMIN-PAGES.md` |
| `ADMIN-PAGES.md` задаёт максимум страницы 200, библиотека допускает 500 | Это не конфликт: HTTP API админки применяет более строгий лимит 200 по A-D101 |
| `IMP-09`, `IMP-11`, `IMP-12` описаны как улучшения, но соответствующие полные контракты отсутствуют в текущем коде | Не считать их готовыми. Список credentials и durable operations идут через A4.1; read queries реализуются публичными методами библиотеки по A-D003/D-122 |

## Правила для следующих задач

1. Сначала добавляйте или расширяйте типизированную read-модель библиотеки, затем HTTP controller.
2. Не возвращайте `StoreTx` rows наружу, если row содержит внутренний или secret-bearing payload.
3. Для каждой мутации применяйте `actor`, `reason`, `Idempotency-Key` и revision contract A-D102.
4. Любой list endpoint использует стабильный cursor и ограничение роли/пространства до чтения данных.
5. Историческая страница не подменяет отсутствующий факт текущим срезом и использует
   `ih_state_snapshot_coverage`, чтобы отличать подтверждённый ноль от отсутствия истории.
6. При изменении страницы обновляйте эту матрицу и финальную трассировку A5.8.
