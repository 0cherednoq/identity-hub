# Прогресс управления назначением прокси

План: [PROXY-ALLOCATION-PLAN.md](PROXY-ALLOCATION-PLAN.md). Решения: `D-337`–`D-352` в [DECISIONS.md](DECISIONS.md).
Запуск реализации: 2026-11-24, по прямому запросу пользователя. Эта очередь не запускает другие очереди.

## Задачи

| ID | Статус | Результат |
| --- | --- | --- |
| PA0.1 | done | Контракты, владельцы TTL, поколения, lock order и стратегия fingerprints зафиксированы в D-337; конкурентное доказательство остаётся за PA1.2/PA5.1 |
| PA0.2 | done | Неизменяемые правила, строгий JSON roundtrip, валидация сервиса, композиция пулов, typing и guard незавершённого runtime |
| PA1.1 | done | Миграция 0044, backfill групп/выданного proxy, FK/индексы и guard отката; проверено на PostgreSQL |
| PA1.2 | done | Общий allocator, три счётчика, владельцы/поколения и configure_group; MemoryStore/PostgreSQL и реальные гонки |
| PA1.3 | done | Admin/batch, импорт policy/trusted, удаление с сохранением истории, scoped doctor и защита endpoint через общий allocator; схема 0046, PG-гонки и regression |
| PA2.1 | done | Atomic reserve/acquire/attach, immediate/existing/transient use, стабильный request, replay и совместимость ключей; schema 0047 |
| PA2.2 | done | confirm_proxy с fence/rules/revision, безопасный DTO, using(session), совместный rollback и PG-гонки |
| PA2.3 | done | Release/revoke/expiry/max_hold/reclaim/forced yield закрывают use; history retention сохраняет владельцев |
| PA3.1 | done | Общий finalizer в полных проекциях; verdict/assignment/claim/outbox атомарны, late claim отвергнут |
| PA3.2 | done | Явный reconcile с текущей Service/override, revision/generation guard, атомарностью и v2 subject-доказательством, включая A → B → A |
| PA4.1 | done | Bounded sweep/lazy recovery, отдельная сверка счётчиков, SKIP LOCKED, честный цикл keyset и retention released; schema 0048 |
| PA4.2 | done | Read-схема v6, физическая ёмкость и pending cleanup, пять событий переходов с очередью TX/savepoint и gauges/идемпотентные event rollups |
| PA5.1 | done | R01–R15 сверены; check.sh: 3068 passed, Store coverage; весь load: 7 passed/8 PERF-1 xfail; три baseline-варианта измерены и повторены |
| PA5.2 | done | Публичный пример, PG commit/rollback и late verdict, API/руководства/rollout/upgrade/CHANGELOG; локальные ссылки проверены |

## Текущий шаг

- [x] Сверить модель назначения с текущими lease/history, binding и fencing.
- [x] Проверить исходные тесты публичного API и здоровья прокси: 22 passed.
- [x] Написать падающие тесты декларативного контракта.
- [x] Реализовать правила, roundtrip и запрет ослабления политики сервиса.
- [x] Добавить typing-контракт и явный отказ runtime до реализации allocator.
- [x] Завершить проверки контрактного этапа: 924 passed, 2 skipped (неустановленные aiohttp/curl_cffi).
- [x] Проверить миграции/metadata/FK/схему на PostgreSQL: 18 passed до добавления guard отката.
- [x] Проверить окончательный guard отката и regression-набор: 126 passed на MemoryStore/PostgreSQL.

PA0.1–PA5.2 завершены. Итоговый аудит PA-R01–PA-R15 и проверки приведены ниже.
Фича готова в рабочем дереве; публикация пакета и production rollout в план не входят.

## Продолжение

Созданы миграции 0044–0048, `SCHEMA_HEAD=0048`, совместима только схема 0048.
0045 добавляет JSON policy, expected_proxy_id и отложенный составной FK владельца резерва.
0046 сохраняет released после удаления ресурсов: исходные ID долговечны, FK вычисляемых active ID
защищают reserved/bound/draining. Downgrade с осиротевшими записями истории требует retention.
0047 хранит proxy_request в lease/history; снимок живой выдачи переживает истечение ключа.
Старые привязки/занятость сохранены, режим backfill-групп — bindings. Strict-выдача включена в PA2.1.
Прежний guard заменён проверкой opt-in группы и реальной atomic reservation. Тест контракта
проверяет отсутствие ранней постоянной пары, release резерва и обратное включение legacy.
Пользовательский незакоммиченный `docs/isue/checker-account-feedback.md` не изменялся.

Pool хранит исходный override и выдаёт отдельный снимок Service/policy. Runtime refresh не наращивает
прежнее дерево правил и не меняет старый Lease; `_capacity_owner` сохраняет общий capacity state.

## Проверки среды

Первый общий unit-прогон: 904 passed, 20 ошибок fixture из-за недоступной системной temp-папки.
Повтор с `--basetemp test-results/pytest-proxy-pa0-20261124`: 924 passed, 2 skipped.
Это изменение места временных файлов, тесты не ослаблялись.
Ruff, форматирование, mypy src/tests/typing и три import-linter контракта проходят.

Для PostgreSQL создан локальный compose-контейнер и отдельная база `ih_proxy_allocations_20261124`.
Docker исчерпал адресные пулы; тестовый override `test-results/proxy-postgres.override.yml`
использует существующий bridge, не удаляет чужие сети. Обычная база `ih` содержит промежуточную
редакцию миграции 0044 из разработки; для финальной проверки использовать выделенную базу.
До опубликованного релиза миграция 0044 ещё может уточняться, поэтому проверки воспроизведения
выполнять на свежей базе/схеме, а не ожидать повторного применения уже отмеченной 0044.

Финальный regression-прогон: `IH_TEST_BACKENDS=memory,postgres`, unit-политики, public/space surface,
conformance proxy assignment/management/enabled/unbind/idempotency, PostgreSQL migration/metadata/FK/
schema/lease lifecycle/schema check: **126 passed**, 140.28 с. Downgrade с активным назначением
отклонён без изменения схемы, после удаления назначения откат сохраняет старую привязку.

Повтор всех unit-тестов и public/space surface после окончательного изменения моделей:
**895 passed, 2 skipped**, 54.05 с. Пропуски относятся к необязательным aiohttp/curl_cffi.
Финальные ruff check, ruff format --check, mypy src/tests/typing, import-linter и git diff --check
зелёные; локальные ссылки плана и файла прогресса проверены.
Созданный для проверок compose-контейнер повторно запущен для PA1.2/PA1.3; данные сохранены.

## PA1.2 и текущая часть PA1.3

Общий allocator выполняет reserve/attach/confirm/detach/close_use, атомарно изменяет ledger и
bound/reserved/draining. Подтверждение не занимает второго места. Владение проверяется полным fence;
постоянный binding и released освобождают owner FK. Новая аренда очищает указатель старой.
Возможна вставка резерва до самого lease с заранее известным ID в одной транзакции; отсутствующий
владелец откатывает commit на обоих Store. Конфигурация использует shared/exclusive advisory-барьер
пространства перед idempotency и требует drain всех его живых аренд. SQL остаётся в PgTx.

Публичные rebind/unbind проверяют optional expected_binding_id. Rebind — явная доверенная команда
администратора с actor/reason, а не подтверждение проверки. Старое использование после замены остаётся
draining; заполненная цель не теряет прежний binding. Rebind_many использует тот же лимит и savepoint;
до обработки строк блокирует все identity, затем весь отсортированный набор proxy пачки.
Disable(unbind=True) отменяет резервы; резерв замены на выключаемом proxy не снимает старую пару
другого proxy. Повторная проверка множества identity после proxy-lock исключает обратный захват.
Смена адреса/credentials и перенос строгого proxy требуют отсутствия резервов/draining/живых uses.
Переход между режимами выполняется configure_group, а move сохраняет режим учёта.
Обычный импорт deferred-сервиса с явно указанным proxy теперь отклоняется confirmation_required;
импорт аккаунта без proxy доступен. Доверенный режим добавлен следующей частью PA1.3 ниже.

Проверки PA1.2: **67 passed** (общий kernel, миграции, PG-гонки, ошибки/каталог событий).
Проверки текущего PA1.3: **87 passed** (MemoryStore/PostgreSQL, admin, kernel, PG-гонки,
proxy batch/enabled). Отдельно новая проверка запрета раннего deferred-импорта: **11 passed** на memory.
Широкий regression после подключения admin: **1107 passed, 2 skipped, 1 failed** за 264.63 с;
единственный отказ — новая фикстура сервиса не вызвала hub.start() для синхронизации PostgreSQL.
Фикстура исправлена. После этого и усиления блокировок batch выполнен итоговый целевой прогон:
**153 passed** за 118.64 с — все proxy conformance, batch contract на обоих Store и шесть реальных
PG-гонок, включая встречные пачки. Запрет раннего импорта прошёл и на PostgreSQL.
Ruff check/format (578 файлов), mypy (183 файла), три import-linter контракта и git diff --check зелёные.
Два пропуска широкого прогона — необязательные aiohttp/curl_cffi.
Полный scripts/check.sh и окончательная матрица требований остаются PA5, фича ещё не завершена.

## Завершение PA1.3

- `imports.run/run_files/stage` принимают `proxy_binding=policy|trusted` и proxy_binding_reason.
  Trusted требует actor/reason до чтения входного потока. ImportRecord не задаёт доверие себе;
  stage сохраняет параметры команды в зашифрованном payload, старые задания означают policy.
- Identity вставляются до proxy-lock. Proxy вставляются/блокируются детерминированно; отбор
  ёмкости учитывает фактическую группу дедуплицированного URL, затем strict binding записывается
  общим rebind. Нехватка места и зависимые от отказавшей цели связи не отменяют независимые записи.
  Хук приложения и назначение откатываются вместе. Legacy импорт сохраняет прежний режим.
- Delete identity с revoke завершает назначения до lease/history-lock в одной транзакции.
  Delete proxy с живым use отклоняется даже с unbind=True. После окончания use завершает
  reserved/draining и сохраняет released с прежним поколением. Batch delete заранее блокирует
  все identity/proxy. Атомарный отказ возвращает ресурс, binding и счётчики.
- 0046 вводит вычисляемые active_identity_id/active_proxy_id с FK и индексами, отдельный space FK.
  Released остаётся доступен после удаления обоих ресурсов. Raw delete с активным назначением
  отвергается FK. Downgrade не удаляет историю и отклоняется до retention осиротевших записей.
- Doctor использует общий reconcile_counters под proxy-lock и scoped configuration-барьером;
  восстанавливает три счётчика по ledger, диагностирует несогласованность пар без предположений.
  Очистка expired uses остаётся отдельной задачей PA4.1. Изменение endpoint читает исходные
  поля уже под proxy-lock, исключая fingerprint от устаревшего адреса.

Проверки: **71 passed** для импорта/фоновых заданий/хуков; **26 passed** для удаления, миграций,
metadata и PG-гонок. Расширенный regression: **1158 passed, 2 skipped, 1 failed** за 345.84 с.
Единственный отказ указал на отсутствующий индекс нового space FK; индекс добавлен в migration/metadata
и текущую тестовую БД. После исправления: **19 passed** (миграции из свежей схемы, metadata,
целостность FK/индексов, tombstones/downgrade и reconcile на обоих Store), без warnings computed defaults.
Ruff check/format (582 файла), mypy (184 файла), три import contracts и git diff --check проходят.
Два пропуска — необязательные aiohttp/curl_cffi. Все восемь PG-гонок прошли в расширенном regression,
включая import против reserve и удаление proxy между выбором кандидата и его блокировкой.

## PA2.1 и обычный release

`proxy_issuance.py` выделяет снимок запроса и выбор ресурса из lease.py. Новый lease ID известен
до reserve; reservation, lease/history, attach и immediate promotion фиксируются вместе. Deferred
не пишет постоянную пару, transient не создаёт новой пары, собственный bound можно использовать
без второго места. Старую legacy-пару при opt-in строгой группы сохраняем до confirm нового резерва.
Привязка strict учитывается даже через pool policy с legacy-группой. SHARED strict выдача запрещена.
Legacy claim сначала выбирает ID без списания, затем под полным набором proxy-lock занимает именно его;
прежняя семантика override max_identities сохранена. Истёкшие uses этой identity чистятся под общим
набором блокировок перед новым reserve; групповой cleanup остаётся PA4.1.

Proxy request относится к lease/use, а не к вечному bound-поколению: admin binding может не иметь
политики и затем использоваться разными пулами. Lease/history хранят request и allocation pointer;
MemoryStore поддерживает те же поля. Lease предоставляет snapshot proxy_allocation_id/state.
Runtime revision и оригинальный override стабильны для Lease; title-only revision тоже конфликтует
при replay. Снимок старого Lease не изменяется при новом acquire. Scalar policy assignment на Pool
учитывается как новый override. Capacity state остаётся общим через `_capacity_owner`.

Known strict primary использует расширенный fingerprint и allow_legacy=False. У legacy сохраняется
первичный fingerprint; если actual proxy строгий, receipt v2 обязательно содержит полный request.
Старый failed retry без доказанного request не обходит проверку через claimed=True. Receipt v2
проверяет format, lease/identity IDs и request; replay проверяет full fence, читает исходные
proxy/allocation, включая draining после admin rebind. Если ключ истёк раньше lease, request из
lease всё ещё проверяется и receipt восстанавливается. Старые version=0 и несовместимые v1
отклоняются без выдачи. Downgrade 0047 с живым request-use запрещён; после drain разрешён.

Обычный release закрывает reserved/draining до lease/history-lock. Исключение завершающего lease
разрешает лишь полный fence; чужой token, space или fencing не освобождают место и не завершают lease.
Поздний release сохраняет резерв нового владельца. Bound остаётся. Это часть PA2.3, не полная её реализация.

Проверки PA2.1: **90 passed** (оба Store, atomic acquire/replay и первоначальная migration/race матрица);
**31 passed** (старые ключи и durable request на PostgreSQL + выдача); **74 passed** (read/admin,
совместимость, FK/metadata/downgrade и публичные гонки). Широкий regression: **1146 passed,
2 failed, 2 skipped** за 369.83 с; оба отказа — один mapper LeaseHistoryRow → LeaseReadRow после
добавления внутренних полей. Mapper разделён на обоих Store. После исправления — **882 unit passed,
2 skipped**, затем **762 conformance passed** с IH_TEST_BACKENDS=memory (две parity/search группы
сами запускают также postgres). **134 passed** за 309.43 с: все PostgreSQL lease/fence/yield/
max_hold/heartbeat conformance, выдача, старые ключи, migration и девять PG-гонок, включая public acquire.
Дополнительно **4 passed**: rollback после immediate promotion/outbox на PostgreSQL и runtime snapshots/
title-only revision/старые ключи MemoryStore. Ruff check/format (587 файлов), mypy (186 файлов),
три import contracts и git diff --check проходят. Полный scripts/check.sh остаётся PA5.

Команде полного conformance нужен IH_TEST_DATABASE_URL даже при IH_TEST_BACKENDS=memory:
test_account_search/test_memory_parity параметризуют оба Store самостоятельно. Без URL fixture
пытается запустить testcontainers, которому недоступен Docker pipe. Повтор с указанной существующей
тестовой БД дал 762 passed; новый контейнер/расширение разрешений не понадобились.

## PA2.2 и завершение PA2.3

`lease.confirm_proxy(reason=...)` и `lease.using(session).confirm_proxy(...)` возвращают неизменяемый
`ProxyAssignment` без секретов. Подтверждение использует canonical request конкретной аренды,
полный fence, текущую Service/group revision, generation и health. Предикаты читают принятые факты
с теми же defaults, что машина состояний. Negative rule запрещает ручной confirm без мутации;
автоматическое снятие остаётся PA3.2. Повтор сохраняет updated_at, одно событие и один слот.

Standalone меняет snapshot Lease после commit. Адаптер SQL-сессии не меняет исходный Lease даже
после commit: возвращённый DTO относится к результату внутри внешней транзакции. Rollback и
вложенный savepoint отменяют состояние, назначение, счётчики и событие вместе; повтор в той же
сессии работает. Явный public observe уважает using(session), внутренний network observe сохраняет
own=True. Проверены оба способа принятия состояния: override и observe.

Revoke/forced yield/expiry/max_hold/reclaim закрывают reserved/draining до lease/history-lock;
bound сохраняется. Expire берёт identity через SKIP LOCKED и все proxy пачки до первого изменения
lease/history. Reclaim без proxy policy тоже согласует старый use, но не закрывает будущий резерв,
который reserve уже вставил той же транзакцией. Heartbeat продлевает только canonical lease,
max_hold ограничивает срок резерва без второго TTL. Retention сохраняет owner history даже если
прежний runtime успел закрыть её до согласования ledger. Поиск owner идёт по индексу до history-lock.

Проверки: **28 passed** (confirm, обе модели Store, внешняя сессия и runtime snapshots);
**30 passed** (новые окончания use на обоих Store); **24 passed** до единственного отказа SQL-plan
retention. Исходный NOT EXISTS планировщик превращал в полный scan ledger; точечный scalar lookup
с LIMIT 1 устранил его. После исправления **48 passed** (SQL-plan, retention, окончания use).
После добавления heartbeat/max_hold и public observe **61 passed** (оба Store и SQL-сессии).
Все четыре реальные PG-гонки confirm/revoke и confirm/admin-replace доказали ожидание identity-lock
через pg_blocking_pids и прошли в обоих порядках. Поздний owner не подтверждает старое поколение.

Широкий regression всех unit и conformance с IH_TEST_BACKENDS=memory и указанной тестовой БД:
**1675 passed, 2 skipped**, 123.67 с. Два пропуска — необязательные aiohttp/curl_cffi.
Ruff check/format (592 файла), mypy (187 файлов), три import contracts и git diff --check зелёные.

Дополнительный PostgreSQL regression: **105 passed** до ошибки fixture чтения pg_stat_user_tables.
Тест выбирал одно имя таблицы из нескольких схем; статистика/reloptions теперь адресуются через
regclass, pg_indexes — через current_schema(). HOT-проверка прошла. В следующем хвосте **7 passed**
до такого же нескоупированного запроса индекса maintenance; он тоже уточнён по current_schema().
После исправления **23 passed** за 44.59 с: maintenance/lock order и все окончания strict-use,
включая новый abandoned-worker сценарий. На memory этот сценарий также прошёл отдельно.
Он доказывает консервативный отказ второму аккаунту до cleanup, возврат места после expire и
безопасный поздний release прежнего владельца. Runtime не менялся из-за ошибок тестовых запросов.

## PA3.1 и автоматические правила PA3.2

`proxy_finalization.prepare/finalize` вызывается из внешней границы полной проекции/команды.
Login, verify/adopt, revalidate и sync сначала принимают итоговые state/session/lifecycle, согласуют
назначение, затем терминализуют provider claim той же транзакцией. Lease verification и
mark_authenticated используют тот же hook. Существующий projection_failed сохранён; отказ commit
после превращения reserved в bound откатывает binding, validity marks, счётчики и событие.
Потерянный fence или истёкший claim не принимают поздний verdict. Assignment/policy conflict
сохраняется как безопасный provider result `proxy_assignment` вместе с принятым verdict.

APPLIED/UNCHANGED дают принятое состояние. Pin/stale/sticky/неизвестный статус не являются
новым доказательством. В составной fault-проекции учитываются решения всех регионов: pin auth
не скрывает принятый banned в standing. Критерий — декларативный факт, а не успех сетевой операции:
принятый limited из классификатора может подтвердить резерв, если именно limited объявлен bind_when.
Технический сбой без принятого изменения не закрепляет и не снимает пару. Acquire сам не подтверждает.
Отрицательное правило имеет приоритет и отменяет также резерв замены, оставляя live use draining.

State/verification overrides, review, timers (в том числе expired challenge/abandoned login),
imports.update_session и новые импортируемые записи, credentials.reset_auth, fault/cooling/pacing
вызывают общий hook после итоговых полей. Batch сначала блокирует все identity/proxy, включая
старые назначения обновляемого импорта. Read/status и техническое восстановление отменённого login
не становятся новым положительным verdict. Lease без выданного allocation не имеет snapshot
поколения и не получает права снять новую административную пару.

Проверки: **26 passed** для первой матрицы встроенных flow на обоих Store; **65 passed** для
расширенной атомарности с confirm/session-контрактами. Широкий regression всех unit/conformance:
**1695 passed, 2 skipped**, 121.35 с (IH_TEST_BACKENDS=memory, обязательные parity/search PG тоже
запущены через указанную тестовую БД). Отдельный PostgreSQL regression: **110 passed**, 175.66 с —
auth/revalidate/session-first, proxy import, external session, allocator и lock-order races.
После дополнения timer/fault aggregation: **42 passed** на memory, **62 passed** на обоих Store
с PG logins и **59 passed** за 61.57 с в окончательной матрице predicates/timers/PG batch race.
Последняя гонка синхронизирует вход обеих пачек перед identity-lock, не использует sleep:
state override_many и встречный rebind_many завершаются без deadlock с согласованными counters.
Неправильные новые fixtures (ImportRecord.sessions и ожидаемый LeaseRevoked при release после
revoke) исправлены; runtime ради них не менялся. Ruff/format (595 файлов), mypy (188 файлов),
три import contracts и git diff --check проходят.

Итоговые проверки после timer hooks и агрегации fault-проекции: **884 unit passed, 2 skipped**,
57.16 с; **818 conformance passed**, 70.38 с. Использованы отдельные basetemp в test-results и
выделенная PostgreSQL БД для обязательных parity/search fixtures. Последние проверки ruff,
format, mypy, import-linter и git diff --check проходят.

## Завершение PA3.2

`proxies.reconcile_bindings` принимает выбранные identity, текущий Service, новый необязательный
pool override, actor/reason, expected_service_revision, полную expected_bindings и atomic. Возвращает
обычный BatchResult, уважает using(session). Все identity/proxy пачки блокируются до первой строки.
Ограничения Service компонуются с новым override; старое нормализованное дерево не наращивается.
Одинаковые policy/revision не меняют hash/updated_at и не создают повторного события. При изменении
hash обновляется через allocator.revise_policy без изменения generation/слота/counters. Затем общий
finalizer проверяет отрицательное правило. Reconcile не создаёт binding и не подтверждает reserve.
Политики draining старого поколения исключены из правил новой пары.

Inner proxy request v2 хранит отдельный allocation_policy_fingerprint. Ledger остаётся декларацией,
per-use subject-поле не входит в её hash. Reconcile добавляет reconciliation_id в ledger policy,
поэтому A → B → A не восстанавливает старое доказательство. Confirm/finalizer проверяют текущий
hash и известную версию. Replay возвращает старый ресурс с прежним subject, даже после истечения
ключа; прав на новую политику не добавляет. Отсутствующий subject не равен None административного
bound. Bound с известной ledger policy v1 может выдать v2 use; live request v1 для подтверждения
нуждается в новой аренде. Неизвестная/нецелая версия не переинтерпретируется. Schema head остаётся
0047, перед обновлением strict-писателей нужен drain. Событие policy_reconciled не подменяет
binding.changed; при снятии пары фиксируются оба. API/typing/guide/CHANGELOG обновлены.

Проверки: **63 passed** на memory после разделения snapshots; **99 passed** на обоих Store для
reconcile/acquire/confirm/старых ключей/SQL-сессии; **46 passed** для revision, constraints, batch и
внешнего rollback; **106 passed** для позднего provider verdict и PG-гонок в обоих порядках.
Широкий regression всех unit/conformance: **1728 passed, 2 skipped**, 118.50 с, с указанной тестовой
PostgreSQL БД для обязательных parity/search fixtures. После новых compatibility-кейсов:
**82 passed**, 96.52 с — v1 bound/v2 use, отсутствие/тип subject version, отрицательное правило
для bound/reserved, actual revision, session rollback/commit, обе PG-гонки и migration/idempotency.
Единственный отказ новой runtime fixture был попыткой зарегистрировать template после start;
fixture использует отдельный Hub с template до start, runtime-ограничение не ослаблялось.
Ruff/format (598 файлов), mypy (189 файлов), три import contracts и git diff --check зелёные.

По итогам PA3.2 шаг PA4.1 был определён как ограниченная очистка reserved/draining вне выбранной identity.
Сейчас expire/reclaim закрывают обычный брошенный use; нужен отдельный обход orphan/старых ledger
и lazy group cleanup, чтобы заполненный proxy со старым чужим резервом возвращался в выдачу.
Чистить чужие identity внутри acquire после уже взятой identity/proxy-lock нельзя: отдельная
короткая cleanup TX должна предшествовать основной выдаче. Discovery и попытки SKIP LOCKED
ограничиваются фиксированным числом кандидатов; пропущенные строки повторяются следующим обходом.
Курсор с верхней границей цикла исключает голодание старого draining при постоянном притоке новых
резервов. Нужен индекс keyset-обхода (в metadata пока нет cleanup index), не полный scan live ledger.
После identity идут отсортированные proxy; занятый proxy можно пропустить, но нельзя исключить
живого владельца из use. Курсор продвигается только после commit. Sweeper нельзя добавлять в ту же
TX после expire_leases/history-lock: Maintenance._leases должен завершить её до новой cleanup TX.
Также нужен retention released после max(lease_history, operation_keys) и исчезновения ссылок,
без вечного удержания из-за оставшейся закрытой строки ih_leases. Старое подтверждение не должно
получать вид нового неизвестного резерва. PA4.2 и полный scripts/check.sh/аудит остаются незавершёнными.

## Завершение PA4.1

`ProxyRecovery` обходит pending ledger и proxy отдельными keyset-циклами с фиксированной верхней
границей. Курсоры CAS продвигаются после commit; rollback оставляет прежнюю страницу, рестарт
безопасно начинает обход заново. Discovery и число попыток блокировки ограничены limit. Group
фильтруется после bounded discovery без сканирования всей группы. MemoryStore использует heap
размером limit; PostgreSQL — индексы space/id и pending space/id из миграции 0048.

Сначала отдельная TX сверяет счётчики строгих proxy под SKIP LOCKED proxy-lock. Она исправляет
завышенный счётчик даже без pending ledger, заниженный — до попытки освобождения. Повреждённое
отношение binding/ledger сохраняет консервативный отказ; legacy автоматически не исправляется.
Затем новая TX берёт identity пачки и proxy по ID с SKIP LOCKED и вызывает общий close_use с
повторной проверкой canonical lease. Продление и confirm после discovery сохраняются. Bound остаётся.

Maintenance._leases завершает expire TX до обеих recovery TX; normal expiry закрывает history
даже для bound use. Публичный `space.maintenance.cleanup_proxy_allocations(limit=100, group=None)`
возвращает число освобождённых назначений, не входит в caller using(session). Отчёт maintenance
разделяет proxy_allocations/proxy_counters. Lazy acquire при первом no_proxy откатывает выдачу,
пробует до 16 ledger/proxy кандидатов и повторяет acquire только при восстановлении ёмкости;
candidate budget этой неудачной попытки возвращается. Успешная выдача не запускает sweep.

Daily retention удаляет released после max(lease_history, operation_keys), при отсутствии
сохранённой истории, binding и живого canonical pointer. Закрытый pointer не удерживает запись.
Partial released/history indexes обеспечивают точечную проверку ссылок. Generic prepared plan
выявил полный scan DELETE-цели с IN(subquery): для allocation retention используется ограниченный
массив ID как initplan и PK lookup. SQL literals состояний сохраняют partial-index planning.

Проверки этапа: **42 passed** — recovery на обоих Store, реальные identity/proxy locks, heartbeat,
retention, maintenance, heartbeat HOT; **40 passed** — generic plans на 5000 строк, migrations и
metadata parity, proxy upgrades/downgrades, повтор recovery на обоих Store. После добавления гонки
confirm после discovery, skip-locked counter repair и заниженного счётчика: **80 passed**, 82.71 с —
оба Store, lease endings и maintenance timers. Ruff/format (603 файла), mypy (191 файл), три import
contracts и git diff --check проходят. API, CHANGELOG, lock-order и D-348 обновлены.

Широкий regression после этапа: **1749 passed, 2 skipped**, 125.87 с — все unit/conformance на
MemoryStore плюс обязательные PostgreSQL parity/search fixtures, с отдельной тестовой БД.
Пропуски — необязательные aiohttp/curl_cffi. Это не заменяет полный PG-набор и scripts/check.sh PA5.
Финальный PostgreSQL-прогон: **23 passed**, 82.08 с — upgrade/downgrade индексов 0048 с
сохранением tombstone, generic query plans, реальные recovery locks/гонки, schema guard и heartbeat HOT.

По итогам PA4.1 следующим шагом PA4.2 были определены selectable proxy_allocations и версия, DTO/API/admin со счётчиками
bound/reserved/draining/occupied и pending cleanup, доступная ёмкость и режим группы. Существующий
proxy_bindings остаётся постоянными парами. События переходов должны исходить из allocator, быть
атомарными и не дублироваться при replay; запись outbox не должна добавлять ранние захваты после
proxy/до lease-history в reserve. Нужен согласованный flush и корректный rollback savepoint, если
будет выбрана отложенная запись событий. Метрики не должны принимать identity/lease/proxy ID labels.
Полный scripts/check.sh, нагрузочный baseline и матрица PA-R01–PA-R15 остаются PA5.

## Завершение PA4.2

ReadSchema v6 содержит proxy_groups/proxy_allocations, binding_id и ссылки use в lease/history.
Allowlist исключает token, policy JSON, fingerprint и credentials. Постоянные пары не смешиваются
с reserved/draining. Proxy list/admin card/group показывают bound/reserved/draining/occupied,
capacity_accounting и cleanup_pending_count. Available/full учитывают всю физическую занятость;
unbound продолжает означать отсутствие постоянной пары. Pending cleanup не уменьшает occupied.
Legacy bound читается как прежнее число пар, strict использует свой счётчик admission. Краткие
get/create/ensure/update DTO сохраняют nullable counters. API/read/typing проверены на обоих Store.

Allocator создаёт события reservation.created/confirmed/released и allocation.draining/released.
Данные — ID поколения и envelope-ресурсов, коды причин, revision и длительности; технический actor
proxy_allocator, без raw exceptions/policy/URL/token. Binding.changed остаётся аудитом постоянной
пары с actor/reason команды. Lease ID nullable, если переход не относится к конкретному use.
StoreTx.defer_events очищает draft через scrub; Store.transaction делает flush перед окончанием
TX или savepoint using(session), после lease/history. Внутренний savepoint копирует/восстанавливает
очередь при rollback. Повтор перехода ничего не добавляет. Reserved → draining считает отмену
один раз, дальнейшее release — физический слот. События резерва сохраняют свой взаимный порядок,
но могут следовать за обычным binding.changed той же команды. Потребитель использует generation.

Обнаружено, что current_lease намеренно исключает expired. Для метаданных expiry добавлен
proxy_use_lease, который читает canonical row этой генерации и после expiry, без новой модели TTL.
Если row отсутствует, фиксируется orphaned без выдуманного дедлайна. Это чтение не разрешает
владение, не меняет heartbeat и не касается lease/history индексов.

MetricsReporter даёт групповые gauges slots по состояниям и cleanup_pending. Resource ID labels
запрещены. Отказы попыток acquire/confirm/finalize используют failures_total с различимыми scope/
reason; эти best effort наблюдения не откатываются. Коммитные события дают идемпотентные analytics
ряда переходов, expiry освобождённых временных слотов, sum/count длительности резерва и задержки
cleanup. Реестр analytics.series принимает эти метрики с измерениями service/group.

Проверки: **32 passed** — чтение на обоих Store, PG read-schema/allowlist/legacy; **34 passed** —
события, savepoint/outer/application rollback, метрики и SQL join; **63 passed** — события/метрики,
recovery, реальные locks и analytics/rollups. Широкий unit/conformance memory regression:
**1761 passed, 2 skipped**, 122.49 с; пропуски — необязательные aiohttp/curl_cffi.
PostgreSQL proxy/races/sessions/lock-order/read-schema/metrics/heartbeat HOT: **73 passed**, 156.22 с.
Первый широкий PG-прогон выявил тестовый barrier, считавший recovery TX третьим acquire-конкурентом:
он теперь считает две именованные задачи acquire. Проверка единственного победителя сохранена,
добавлена проверка единственного reservation.created. Runtime ради этого теста не ослаблялся.
Ruff/format (605 файлов), mypy (191 файл), три import contracts и git diff --check зелёные.
DDL head остаётся 0048. D-349, API, proxy/events guide, integrations, CHANGELOG и lock-order обновлены.
Финальный proxy conformance на **обоих Store: 419 passed**, 351.14 с. Включает acquire/replay,
confirm, admin/import/delete, finalization/reconciliation/recovery, read/events/metrics, а также
новую проверку отсутствия reservation.confirmed после потерянного provider claim.

Следующее PA5.1: собрать проверяемую матрицу PA-R01–PA-R15 по текущим исходникам и тестам, проверить
все assignment-пути и guards миграции/старых ключей, прогнать scripts/check.sh и нагрузочный протокол.
Отдельно исправить orphan history для bound: нормальная expiry уже закрывает историю до ledger
sweep, но probe `test-results/pa5_orphan_probe.py` воспроизвёл удаление canonical row с оставленной
открытой history на MemoryStore. После Maintenance._leases результат history_open=True,
bound_count=1, in_lease_count=1, leases/proxy_allocations/proxy_counters=0. PostgreSQL expire_leases
начинает с inner join canonical leases/history и также не обнаруживает отсутствующий row; отдельная
bounded очистка orphan history нужна до завершения R14. Существующая orphan fixture закрывает
history сама и этот случай не доказывает. Binding снимать не нужно, независимый TTL придумывать
нельзя. Discovery/skip locks/history close требуют отдельного bounded прохода в обычном порядке
identity/proxy → history, с повторной проверкой canonical generation. Проверить также принятую
техническую ошибку против
положительного bind_when и полные границы accepted projection. После этого PA5.2: потребительские
проверяемые примеры и coordinated rollout. Цель целиком остаётся активной.

Для scripts/check.sh доступны `C:/Program Files/Git/bin/bash.exe`,
`C:/Users/user/.local/bin/uv.exe` и каталог `.uv-cache`. Обычные PATH bash/uv ведут в WSL/mise
shims; нужно использовать проверенные бинарники и локальный UV_CACHE_DIR. Pytest — с
`-p no:cacheprovider --basetemp test-results/<новая-папка>`, отдельной тестовой PostgreSQL БД
и последовательными PG-шардами; не запускать два процесса, чистящих общую БД, одновременно.

## PA5.1 — аудит и исправления

Предыдущий goal turn изменил код/контракты/тесты PA4.2 и подтвердил их прогонами: это progress.
Аудит PA5 выявил два воспроизводимых случая, которые нужно закрыть до итоговой приёмки.

1. Orphan history с потерянной canonical row исправлена. Третий отдельный проход ограничивает
   discovery и попытки SKIP LOCKED, повторно проверяет canonical lease ID и закрывает только
   managed history. Binding/новый use сохраняются. Существующий open-space index подходит;
   head остаётся 0048. Probe теперь даёт history_open=False, bound_count=1, in_lease_count=0,
   proxy_history=1. **50 passed** на обоих Store с recovery/locks; затем **26 passed** для новых
   исторических случаев, двух sweeper, identity/history locks, нового use после discovery,
   удалённой identity, generic SQL plans на 5000 open history и heartbeat HOT. D-350 описывает rank.
2. Probe `test-results/pa5_technical_probe.py` показал bind по прежнему auth при новом cooling
   от технического fault. Finalizer теперь проверяет новое доказательство из принятой проекции
   для удовлетворённой ветки bind_when. Исторические факты остаются предусловиями AllOf; AnyOf
   требует доказательства в совпавшей ветке. Значение доказательства должно совпадать с итоговым
   снимком. Pin/stale не дают доказательств; отрицательное правило имеет прежний приоритет.
   Guard относится к promotion reserved, существующий bound сохраняет идемпотентный исход.
   Принятые факты переданы из login/verify/adopt/revalidate/sync, verification, mark_authenticated,
   cooling и классификатора fault. Verify VALID доказывает только проверенный слот. Ручной confirm
   остаётся явным доверенным решением приложения с проверкой canonical правил и fence.
   Probe теперь оставляет reserved. **66 passed** — новые ветки, provider finalization/reconcile и
   unit proof на MemoryStore. Последующий прогон подтвердил новые proof-сценарии на обоих Store.

Начат scripts/check.sh с IH_TEST_BACKENDS=memory,postgres и IH_STORE_COVERAGE=1. Первый шард:
**863 passed**, 777.06 с. Во время второго обнаружен R12 и изменён runtime; текущий запуск отражает
промежуточный снимок, итоговый gate нужно повторить после всех исправлений. Запуск завершился:
второй шард **831 passed**, третий — **4 failed, 1348 passed, 4 skipped, 1 deselected**.
Обнаружены два capacity event regression, гонка выбора proxy и SQL-бюджет импорта; также Store
coverage потребовал восстановить прямое покрытие прежних insert_proxy/unbind_identity.
Новый conformance raw-helper контракт проверяет legacy идемпотентность и отказ strict bypass.
Повтор новых proof/examples/raw helpers и прежних regression: **3 failed, 18 passed**; ошибки
ёмкости/импорта повторились, новые PG proof и публичные примеры прошли.

Все три причины исправлены (D-352): release читает capacity owner; устаревший выбор proxy
повторяется в новой TX; импорт свежих строк без proxy не делает N проверок прежнего назначения.
Исходные тесты/budget не ослаблены. **13 passed** PostgreSQL: aggregates, bindings, import volume
и новый barrier выбора одного proxy пятью работниками при суммарной ёмкости четыре, в обоих режимах
и с candidate budget=1. Ruff imports и mypy (191 файл) проходят после исправлений.

Публичный пример `examples/proxy_assignment.py` использует встроенный authenticator, ручной
checker, unknown/challenge, последующий invalid и атомарную caller-owned SQLAlchemy TX.
Standalone MemoryStore запуск и conformance на обоих Store прошли. Два PG-теста подтверждают
совместные commit/rollback приложения и confirm. В примере нет `_tx` и записи в таблицы Hub.
Руководства services/leases/proxies/limitations описывают policy composition, client TTL,
coordinated rollout и ограничения challenge. Schema head=0048, ReadSchema.version=6.

Подготовлен `benchmarks/proxy_allocations.py`: отдельные схемы для исходного HEAD, текущего legacy
и allocations, фиксированный источник import, p50/p95/SQL statements, batch heartbeat, contention,
high occupancy, expiry batches, drain, pg_locks и размеры индексов. Baseline src извлечён через
git archive из HEAD `5ba79bddf56964f8508e29285e85a754297f0065` (schema 0043) в test-results.
Все три варианта измерены: [отчёт и бюджеты](PROXY-ALLOCATION-BENCHMARK.md).
Heartbeat остаётся одним SQL на пачку 1/49, contention даёт 1/49, строгий drain не переиздаёт
место до release, полный expiry cleanup оставляет occupied=0, deadlocks=0 во всех трёх прогонах.
Цена strict учёта видна: existing-bound acquire 53 SQL против 11 в HEAD; текущий legacy — 20.
Задержки локального стенда и размеры индексов сохранены без обещания production SLA.
Первые значения зафиксированы как бюджет для последующей оптимизации, heartbeat имеет жёсткую
границу один SQL на пачку. Сам benchmark после первого успешного измерения дополнительно
проверяет число продлённых leases и этот SQL-бюджет; runtime с момента запуска финального gate
не изменяется. Штатный load и scripts/check.sh выполняются последовательно с Store coverage.

### Аудит путей записи R03/R15

`proxy_store_allocation`, `proxy_store_binding` и `proxy_set_counters` вызываются для переходов
из allocator; counter doctor использует тот же модуль под proxy-lock. Ledger backend-методы —
примитивы StoreTx, не новые публичные операции. Поиск прямых binding/count writes сверён с
обоими backend, issuance, importing, identities, proxies и secrets_admin.

| Путь | Граница записи и защита |
| --- | --- |
| Acquire/reuse/deferred/transient | proxy_issuance → reserve/attach_use; legacy claim/bind только для bindings |
| Confirm, автоматическая положительная finalization | confirm_assignment → allocator.confirm; fence, policy subject/revision и generation |
| Unbind/rebind/rebind_many, disable с отвязкой | detach/rebind/unbind_proxy; все proxy команды заблокированы по ID до изменения ledger |
| Импорт/create_many с proxy | managed → allocator.rebind; обычный deferred reject; trusted actor/reason; fresh legacy → guarded bind/load |
| Удаление identity/proxy и batch | prepare_identity_delete/prepare_proxy_delete; закрытие use до удаления, released tombstones и live-use drain |
| Move/endpoint/credentials/secrets rotation | validate_proxy_change под proxy-lock, occupied/use drain; одинаковый accounting для move |
| Release/revoke/expiry/reclaim/yield | close_use в общем порядке identity/proxy до history/provider; bound сохраняется |
| Reconcile/state/timer/provider verdict | prepare/finalize или revise_policy; negative priority, нет отдельного subscriber-писателя |
| Recovery/counter doctor/history | bounded discovery и SKIP LOCKED; canonical перепроверка; orphan history не меняет ledger/counters |
| Старые StoreTx bind/rebind/claim/load/unbind | require_legacy отвергает strict; raw delete identity/proxy требует предварительно закрытого ledger |

Прямой decrement bound_count в legacy delete_identity остаётся после проверки отсутствия активных
allocations; managed deletion сначала делает prepare_identity_delete. Массовый legacy delete_proxy
аналогично отказывает при активных allocations. Низкоуровневые legacy helpers дополнительно
покрыты conformance test_proxy_legacy_store_contract на обоих Store.

### Итоговая матрица требований PA5.1

| Требование | Механизм и доказательство | Подтверждение (пройдено) |
| --- | --- | --- |
| R01 | reserve/_write под identity/proxy-lock; allocator/acquire races; нагрузка 50 работников даёт 1/49 | Финальный gate |
| R02 | Три счётчика с ledger, нейтральный promotion, rollback/doctor; SQL и индексы измерены в отчёте | Финальный gate |
| R03 | Все direct binding/load writes сверены; managed admin/import/delete через allocator; legacy raw guard conformance | Финальный gate |
| R04 | Binding PK identity, allocation ID как generation, expected binding/proxy; admin/delete/migration tests | Финальный общий gate |
| R05 | check_fence, canonical request/subject и exclusive-only; confirm/session/race tests | Полный финальный прогон |
| R06 | ih_leases владеет TTL/hold_deadline; renewal/expiry/max_hold; 1 SQL для 1/49 use, load batch500 прошёл | Финальный gate |
| R07 | Durable proxy_request и receipt v2, replay исходного ресурса; acquire/idempotency/migration tests | Финальный прогон старых ключей |
| R08 | Терминальный released, идемпотентные confirm/close, очередь outbox; events/recovery tests | Финальный gate |
| R09 | Expected generation + policy subject/reconciliation nonce; reconcile/finalization races; proof на обоих Store | Финальный gate, включая late manual example |
| R10 | Canonical live use удерживает draining; rebind/end/recovery; нагрузка доказала отказ до release и возврат после | Финальный gate |
| R11 | provider I/O вне TX, finalization до terminal claim, deferred outbox; claim/session rollback tests | Финальный PG claim/races |
| R12 | Итоговый canonical snapshot, accepted proof нужной ветки, pin/stale; 66 memory/unit и новые PG proof cases | Финальный gate |
| R13 | Read allowlist v6, безопасные DTO/events/errors/metrics; typing; standalone public example без секретов | Финальный gate |
| R14 | Keyset/counter/lazy/orphan recovery, 50/26 tests, SKIP LOCKED revisit и expiry backlog 50 use до occupied=0 | Финальный gate |
| R15 | Opt-in, legacy raw guards/compatibility; HEAD/bindings/allocations benchmark; coordinated rollout/upgrade guide | Финальный gate |

Все указанные проверки пройдены. PA5.1/PA5.2 завершены после успешного check.sh/Store coverage,
нагрузки, повторного benchmark и сопоставления матрицы с окончательным runtime. Существующие
latency-пороги PERF-1 не менялись: восемь xfail остаются описанным ограничением, не выполненным SLA.

### Финальная проверка PA5

Штатный `tests/load` завершён: **6 passed, 8 xfailed**, 382.84 с. Import 10k — 32.713 с,
search 100k p95 — 30.25 ms, event p95 — 57.34 ms. Batch heartbeat 500 use: single 123,
batch100 11 983, batch500 35 198 renewals/s. Восемь исторических D-127 PERF-1 xfail сохраняют
исходные пороги: occupancy/require_quota, 50-worker acquire и operation. Пороги не ослаблялись,
прогон не доказывает выполнение latency SLA. Логи/JUnit — test-results/pa5-final-load.*.

Следом запущен финальный `scripts/check.sh` на memory,postgres, SHARDS=1 и STORE_COVERAGE=1;
ruff/format, mypy и три import contracts прошли. Production runtime зафиксирован до запуска;
после него менялись только документация и пример/его тест перед началом pytest.
В ручном примере INVALID делает явный unbind только проверенного binding generation.
Добавлены normal/late-INVALID сценарии: новое назначение оператора сохраняется. Revision
аккаунта не является поколением binding, общий identities.observe не наследует fence аренды;
это теперь явно описано в guide. **3 passed** MemoryStore для окончательного примера;
standalone запуск выдаёт login=succeeded/manual=bound/unknown=failed/challenge=challenged,
после invalid bound=1/temp=0, без секретов. Финальный PG прогон этих сценариев прошёл в gate.

Финальный gate: шард a–o — **863 passed**, 869.78 с; шард p–z — **849 passed**, 701.49 с.
Второй включает окончательные public examples, late manual INVALID, proof и raw legacy Store
контракты на обоих backend. Завершающий PostgreSQL/unit/root шард — **1356 passed, 4 skipped,
1 deselected**, 986.90 с. Общий `check.sh` завершился с exit=0: **3068 passed**, Store coverage
обоих backend без пропусков abstract methods. Четыре skip — optional aiohttp/curl_cffi и два
PgBouncer-only теста без соответствующего стенда; они не относятся к proxy allocation контрактам.
Source runtime остаётся тем же. Отдельный исключённый из check.sh load-тест throttle recovery
на миллионе history rows: **1 passed, 2 deselected**, 38.04 с. Вместе все load-маркеры дают
**7 passed, 8 xfailed**. Исправленный измеритель throughput/heartbeat повторил все три варианта
последовательно с exit=0. SQL-бюджеты воспроизвелись точно: HEAD 11/8, 12/8; bindings 20/9,
21/9; allocations 53/20, 41/30. Heartbeat 1/49 use — один SQL, число продлённых leases проверено,
contention — 1 winner/49 refusals, deadlocks=0. Strict drain не переиздаёт место до release;
после полного expiry cleanup occupied=0. Отчёты окончательного измерителя —
test-results/pa5-benchmark-{baseline,legacy,allocations}-verified.json.
План закрыт; ограничения exclusive-only, client cancellation, coordinated rollout и цена SQL
остаются частью публичного контракта. Никакие другие очереди этим планом не запускались.
