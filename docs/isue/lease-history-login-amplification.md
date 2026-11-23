# Login amplification, рост `ih_lease_history` и восстановление throttle из audit-журнала

**Статус.** Дефект воспроизведён на `1.0.0a8` и `1.0.0a9`. Исправление реализовано
и проверено в рабочем дереве. Для выката требуется миграция до `0043`.

Приоритет: P0

Исходные данные ишью:

- `identity-hub==1.0.0a8` в production-интеграции `delivery-mail`;
- `identity-hub==1.0.0a9`, commit `f013b5cdd8fd2a6ea35aa82e2e632a999381da97`;
- PostgreSQL 17, снимок от 2026-11-23.

Основной читатель: maintainer `identity-hub`

Тип документа: root-cause и implementation issue

## Вывод ревью · 2026-11-23

`delivery-mail2` использует публичный API по назначению. В `src/delivery/maintenance.py` (строки
112–134, 244–251, 422–429) maintenance вызывает `expire_logins()` и затем
`space.auth.login_many(service=identity.service.name, limit=...)`. Дефолты в
`src/delivery/config.py:98–101` — 5 секунд и 100 identity. Пауза начинается после завершения
действия. Периодический вызов прямо описан в [руководстве по входу](../guide/05-login.md).
Частота усиливает дефект, а увеличение интервала только снижает его частоту. `send_message`
настроен на сервисе Rambler и используется отправкой (`src/delivery/send.py:192`,
`src/delivery/application/sender_pool_service.py:571`). `RamblerAuthenticator.login()` throttle
не вызывает. Разделять один сервис на два ради этого приложению не требуется.

**Вклад адаптера.** `src/delivery/providers/rambler.py:193–217` корректно преобразует
`InvalidCredentials` в `LoginFailure(credentials_rejected, retryable=False, credential=...)`.
`SignInRefused` в `packages/rambler-sdk/rambler_sdk/auth/steps.py:33–41` — fallback для разных
отказов. По имени исключения нельзя установить, что пароль неверен или отказ постоянный.
Разобрать доступные безопасные статусы/коды и типизировать известные отказы следует в адаптере.
Транспортным и нераспознанным ошибкам подходит временный отказ без пометки credentials как
rejected. `Service.error_classifier` относится к операциям под арендой, а вход обрабатывает
`LoginFailure` явно ([D-310](../plan/DECISIONS.md)). Это отдельная задача интеграции.

### Что проверено

Проверены установленный пакет `1.0.0a8` из `.venv` клиента и исходники `1.0.0a9` на
`f013b5cdd8fd2a6ea35aa82e2e632a999381da97`. В обоих случаях использованы публичные `auth.login()`
и `auth.login_many()` с `MemoryStore`, `FakeClock` и тестовым Authenticator. Отдельно подсчитаны
вызовы `get_throttle_state()` и `last_unclean_lease_end()` в `StoreTx`.

| Сценарий | Наблюдение на обеих версиях |
| --- | --- |
| Три identity с rejected/disabled credentials, два вызова `login_many(limit=1)` после начальных отказов | Каждый вызов создал три аренды. Итого 6 новых history rows, 6 `lease.acquired`, 6 `lease.released`, 0 вызовов Authenticator. `get_throttle_state` и `last_unclean_lease_end` вызваны по 6 раз. |
| Пригодная credential, две попытки с `ConnectionError` при одном времени `FakeClock` | Authenticator вызван дважды, обе операции появились в отчётах. |
| `LoginFailure(retryable=False)` без указания пары и два batch-вызова | С одной неизменной active credential адаптер вызван дважды. При отсутствии записей credentials — один раз. Проверка `credential_candidate is True` означает «есть пригодная пара», без проверки её новизны. |
| Держатель занял слот при `interval=5s`, `burst=1`, завершился по `max_hold=3s` без release | Через 3,1 секунды преемник получил слот сразу. В истории стоит `max_hold`, которого нет в predicate recovery. |

Базовый прогон тестов `test_throttle`, `test_import_acceptance`, `test_secret_value_validity`,
`test_login_operations` и `test_login_resilience` с `IH_TEST_BACKENDS=memory` — **28 passed**.
`test_login_operations` фиксирует немедленный следующий batch retry после `ConnectionError`.
Введение backoff меняет это поведение и требует обновить тест на управляемом времени.
Снимок PostgreSQL, цифры и `EXPLAIN` ниже предоставлены автором ишью. Код SQL и индексы сверены
с репозиторием. Production не запрашивался, отдельный прогон PostgreSQL не выполнен.

### Оценка предложения

| Часть | Решение по результатам ревью |
| --- | --- |
| Eligibility до acquire | Принять. Встроить во внутренний `CandidateQuery` и штатный выбор с `SKIP LOCKED`/LRU. Переиспользовать семантику D-317/D-319 и общее правило пригодности без чтения секретов. |
| Ограничение `limit` | Принять как общий бюджет выданных аренд и кандидатов, переданных на повторную проверку после выбора. `limit` не ограничивает число строк, которые планировщик просмотрит при отборе. Вложенный повтор по `_SkipCandidate` должен тратить тот же бюджет. |
| Durable backoff | Принять для `login_many`. Состояние входа, операция провайдера и расписание должны коммититься вместе под claim/fence. Сохранять смысл `LoginFailure.fault`, ручного входа и `AcceptPolicy`. Публичный `retry_after` можно добавить отдельно, когда адаптеру потребуется передавать срок провайдера. |
| Lazy recovery | Принять для обеих веток acquire, включая повтор с `idempotency_key`. Загружать первый TAT для конкретного имени под существующим lock с проверкой fence. Сохранять только занятые слоты. |
| Компактный marker | Принять. Добавить покрытие принудительного `yield` и истечения по `max_hold`. Действующий запрос видит только `expired/lost/revoked`, а `yielded` используется и при добровольной передаче, и при принудительной. |
| Индекс, retention и очистка | Для основного решения новый индекс истории не нужен. Исходные данные остаются источником backfill. Очистку и изменение retention рассматривать после измерения эффекта, отдельно от релиза исправления. |

### План реализации и прогресс

| Этап | Статус | Условие завершения |
| --- | --- | --- |
| Ревью и локальное воспроизведение | `done` | Обе версии воспроизведены, интеграция и контракты сверены. |
| Отбор кандидатов | `done` | Отбор без аренды, проверка истории и событий на обоих Store, тест конфликта захвата и общего бюджета. |
| Расписание повторов | `done` | `retry_at`, явный вход, сохранение запрета при перезаписи той же пары и гонка замены проверены на обоих Store. |
| Ленивый throttle | `done` | Обычный и идемпотентный acquire/release обходятся без throttle I/O. Проверены первая загрузка по имени, `wait=False` и гонка с release. |
| Marker и миграция | `done` | Backfill, нечистые завершения и retention проверены. Отрицательный generic plan на 1 млн history rows читает только marker. |
| Документация и проверка релиза | `done` | Гайды, changelog и upgrade path обновлены. Проверены оба Store, сборка пакета и maintenance `delivery-mail2`. |

2026-11-23 · Отбор кандидатов · `done` · 37 тестов по валидности credentials, acceptance,
операциям провайдера и устойчивости входа прошли на MemoryStore и PostgreSQL.
2026-11-23 · Расписание повторов · `done` · 81 тест входа, валидности и acceptance прошёл,
включая обе реализации Store и существующие тесты PostgreSQL.
2026-11-23 · Throttle · 28 тестов на MemoryStore и PostgreSQL. Проверены все пути нечистого
закрытия, удаление audit history, backfill в пользовательской схеме и отсутствие SQL-чтения
истории при первом `throttle()`.
2026-11-23 · Нагрузочная проверка · PostgreSQL с 1 млн закрытых строк. Под
`force_generic_plan` отрицательный recovery lookup обращается только к `ih_throttle_recovery`.
Повторить можно командой `pytest -m load tests/pg/test_throttle_recovery.py`.
2026-11-23 · Интеграция · 10 тестов maintenance и контракта сервиса в `delivery-mail2` прошли
с локальной версией библиотеки. В трёх штатных проходах maintenance с подставным Rambler SDK
отклонённые credentials не создали новых аренд. `SignInRefused` дал один вызов SDK, одну
аренду и сохранённый `retry_at`, следующие два прохода пропустили аккаунт.
2026-11-23 · Сборка · wheel и sdist собраны локально. Из распакованного wheel обнаружена
ревизия `0043`, оба офлайн-примера отработали. Ruff, mypy и три контракта импортов прошли.
2026-11-23 · Итоговая проверка · 1 336 conformance-тестов и 1 274 PostgreSQL/unit-теста прошли.
Два теста с прежним списком таблиц повторно прошли после добавления новых таблиц в ожидания.
Проверен вызов всех 213 методов Store на каждом бэкенде. Четыре теста пропущены из-за
PgBouncer и опциональных HTTP-клиентов. Дополнительно 27 целевых тестов прошли на PostgreSQL 17.
Production rollout и очистка истории относятся к этапу развёртывания.

**Отбор кандидатов.** В `auth.py::_run_logins` включить login-предикат в текущий запрос на
`probe`. В PostgreSQL использовать коррелированные `EXISTS` по `(space_id, identity_id)` для
наличия любой credential, пригодной credential и текущего региона `auth/default`. В MemoryStore
применить эквивалентные условия. Базовое поведение из D-319 сохраняется: без записей вход
допустим, если прежний отказ его не запретил. Существующие, но целиком негодные записи
исключают identity, пригодная альтернатива разрешает попытку. `Authenticator` может работать
с разными видами credentials, поэтому фильтр Core не привязывается к `default` kind Rambler.
Использовать часы хранилища. После получения блокировки выбранной identity сверить eligibility
перед разрешением прокси и созданием аренды. При изменениях после коммита сохранить проверку
кандидата самим Authenticator и корректное освобождение lease. Тесты должны проверять отсутствие
history, двух lease events и provider operation у отфильтрованной identity, глобальный `limit`
между сервисами и расход бюджета при конфликте acquire. Параллельные batch workers должны
сохранить текущие гарантии fencing и `SKIP LOCKED`.

**Расписание повторов.** Хранить компактное состояние на identity (`retry_at`, счётчик
последовательных временных отказов и привязка к credentials для запрета) в Store, с индексом
или ключом для точечного чтения при отборе. `retry_at` относится к допуску пакетного входа.
`availability_until` распространяется и на другие операции, а provider worker имеет
собственные правила повторов (D-222/D-223). Для первой реализации предлагаются 30 секунд,
удвоение до 10 минут и небольшой случайный разброс внутри предела. Проверять срок как
`retry_at <= tx.now()`. Структурированный временный отказ, исключение адаптера, timeout и
действительно брошенный вход должны назначать срок. Для `LoginAbandoned` повторно проверить в
одной транзакции состояние `logging_in`, срок и отсутствие живой аренды. Отмена вызывающим,
потеря fence и сбой записи не должны становиться подтверждённым отказом провайдера.

Для нового `retryable=False` без credential key запретить тот же набор пригодных пар. При
отклонении конкретной пары оставить возможность пробовать другую пригодную пару согласно
D-319. Снимать относящийся к credentials запрет или backoff после появления или действительной
замены пригодной пары либо явного `reset_auth=True`. Учесть, что `CredentialRow.revision`
увеличивается также при служебных обновлениях валидности. Нужен снимок или отдельная версия
эффективного набора кандидатов, согласованная между путями put, replace, lifecycle и import
merge. Зафиксировать её до сетевого вызова, чтобы поздний отказ для старых данных не наложил
запрет на уже заменённую пару. Для старых `auth.retryable=False` без снимка сохранить текущую
совместимость, а новый запрет записывать после нового отказа. Успех очищает расписание, challenge
передаёт управление существующему challenge flow. Явный `auth.login(id)` обходит gate и пишет
исход попытки по обычным правилам. `AcceptanceWorker` сохраняет свой контракт `max_attempts`
и lifecycle, включая переход к другой паре. Документировать, что `retry_at` ограничивает
`login_many`, и проверить взаимодействие с acceptance, ручным входом и переездом процесса.
Этапы отбора и расписания выпускать вместе.

**Ленивый throttle.** Удалить `with_throttles()` и передаваемый через `LeaseRow` снимок при
новом и идемпотентном acquire. Первый `throttle(name)` в exclusive-режиме читает сохранённый TAT
и recovery bound под per-name lock, один раз на имя даже при отсутствии записи. Перед чтением
проверить владение в Store, после `await` повторно проверить локальное состояние аренды, включая
release/потерю lease во время загрузки. `wait=False` с ненулевой задержкой не занимает слот.
Release записывает только TAT имён с выданными слотами, до передачи живой аренды следующему
держателю. Проверить гонки first call/release/revoke и штатный атомарный путь `SHARED`.
Промежуточную реализацию можно проверить на текущем history lookup, а выпускать вместе с marker.

**Marker и миграция.** Хранить последний нечистый конец на identity в отдельной компактной
строке, например `ih_throttle_recovery`, с привязкой к пространству и каскадом удаления identity.
Эксклюзивные reclaim, maintenance expiration, release с нечистым исходом, revoke и forced yield
обновляют marker монотонно в той же транзакции, которая завершает аренду. Для истечения брать
`expires_at`, для revoke/forced yield — время закрытия. При неудаче или повторе закрытия marker
должен соответствовать реально завершённой аренде. Закрытие истёкшей истории при удалении identity
не требует сохранять состояние для удаляемого аккаунта. Проверить порядок блокировок при
конкурентных acquire/release/maintenance/revoke и обе реализации Store. В audit оставить причины
и времена закрытия, а recovery для первого обращения считать по прежней формуле N4.2.

Миграция PostgreSQL после текущей ревизии `0041` (`0041_runtime_service_definitions.py`) должна поддержать
пользовательскую схему. Backfill — `max(released_at)` по живым identity и сохранённым строкам
`expired/lost/revoked/max_hold/yielded`. Исторические `yielded` брать консервативно, поскольку
журнал не различает два способа передачи. Новые добровольные передачи с сохранённым TAT marker
не двигают. История, удалённая до миграции, невосстановима. Не допустить записи закрытия старым
воркером между backfill и переходом на marker: для этого релиза запланировать согласованную
остановку писателей на время миграции и смены версии. На копии оценить длительность backfill,
а в PG-тесте проверить upgrade с данными, custom schema, retention и оба пути истечения
(reclaim и maintenance). После перехода удалить history lookup из `StoreTx`. Проверить трассой
SQL, что отрицательный recovery делает точечные чтения и не обращается к `ih_lease_history`.
Миллион строк и `force_generic_plan` уместны в нагрузочном профиле или проверке временного
индекса. Основной регрессионный тест может ловить обращение к history на малом наборе данных.

**Проверка релиза.** Дополнить существующие conformance-наборы, PG-тесты гонок и миграций.
В базовой версии `test_expired_holder_recovers_conservative_bound` пропускал PostgreSQL.
Новая матрица должна проверять оба Store. Обновить [гайд входа](../guide/05-login.md),
[гайд аренд](../guide/07-leases.md), [решения](../plan/DECISIONS.md) и changelog. На стороне
`delivery-mail2` проверить штатный maintenance с обновлённой библиотекой и отдельно покрыть
классификацию отказов Rambler. Сравнить число batch login leases с `identity.login.*` и ростом
истории, учитывая отмены и падения. Для первой проверки достаточно существующих событий и
метрик acquire. Исторические production-строки оставить до отдельного решения о retention.

## Материалы исходного ишью

Ниже приведены диагноз, замеры и предложение контрибьютора. Этапы реализации и критерии их
завершения уточнены в разделе «План реализации и прогресс» выше.

### Требуемый результат

`identity-hub` не должен создавать аренду, строку истории и два audit-события для аккаунта,
который заранее не подходит для автоматического входа. Повтор временно неудавшегося входа должен
иметь долговечный backoff.

Восстановление GCRA/TAT после аварийной аренды должно выполняться только при фактическом вызове
`Lease.throttle()`. Корректность throttle не должна зависеть от полного audit-журнала
`ih_lease_history` и его retention.

После исправления должны выполняться следующие инварианты:

1. `auth.login_many()` не берёт заведомо неподходящие identity в аренду.
2. `limit` ограничивает число рассмотренных или арендованных identity, а не только сетевых попыток.
3. Временная ошибка входа не повторяется раньше `retry_at`.
4. Аренда сервиса с настроенным throttle не читает throttle state, пока приложение не вызвало
   `Lease.throttle()`.
5. Первый throttle после падения держателя сохраняет действующую консервативную гарантию GCRA.
6. Очистка audit-журнала не может ослабить throttle.

### Краткий диагноз

Проблема состоит из четырёх усиливающих друг друга дефектов.

1. `auth.login_many()` вызывает `pool.acquire()` до проверки `credential_candidate_state()` и
   `auth.retryable`. Для каждой заранее неподходящей identity создаются аренда, строка
   `ih_lease_history`, `lease.acquired` и `lease.released`.
2. Повторяемая ошибка входа не имеет `retry_at` или backoff. Identity с временной или неверно
   классифицированной ошибкой остаётся доступной на каждом следующем запуске `login_many()`.
3. `pool.acquire()` немедленно загружает throttle state для любой эксклюзивной аренды сервиса,
   если у сервиса существует хотя бы один throttle. Назначение аренды и фактическое использование
   throttle не учитываются.
4. `last_unclean_lease_end()` восстанавливает оперативное состояние из 90-дневного audit-журнала.
   Запрос не имеет подходящего индекса, а generic prepared plan в худшем случае читает всю историю
   пространства.

Медленный запрос создаёт случайный обратный эффект: один проход по проблемным аккаунтам длится
несколько минут вместо заданных приложением пяти секунд. Если добавить только индекс, цикл снова
станет быстрым и сможет генерировать намного больше аренд, событий и внешних попыток входа.

### Production evidence

#### Объём данных

На момент снимка в пространстве было 1 123 identity:

- 1 037 в состоянии `authenticated`;
- 86 в состоянии `no_session`;
- из 86 `no_session` у 75 нет пригодной credential candidate;
- у 11 есть активная candidate.

При этом `ih_lease_history` содержала 1 196 295 строк. Из них 1 190 437, или 99,51%, создал пул
`identity-hub:login`.

| Объект | Строки | Heap | Индексы | Всего |
| --- | ---: | ---: | ---: | ---: |
| `ih_lease_history` | 1 196 295 | 261 MiB | 499 MiB | 760 MiB |
| `ih_events` | около 2,55 млн | 761 MiB | 929 MiB | 1,69 GiB |
| `ih_throttle_state` | 462 | 120 KiB | 48 KiB | 200 KiB |

Статистика таблицы показывает 1 196 256 insert, 1 196 265 update и 0 delete. Каждая аренда
вставляет открытую строку истории, затем закрытие обновляет её. Пространство создано 2026-11-07,
поэтому 90-дневный retention ещё не мог удалить ни одной строки.

#### Скорость роста

За последние полные сутки login-пул создал 24 116 аренд для 82 identity:

- 24 093 завершились `released`;
- 23 завершились `expired`;
- каждый из наиболее активных аккаунтов получил около 295 служебных аренд за сутки.

Дневной рост `identity-hub:login`:

| Дата, UTC | Аренды |
| --- | ---: |
| 2026-11-07 | 18 077 |
| 2026-11-08 | 74 040 |
| 2026-11-09 | 153 919 |
| 2026-11-10 | 100 055 |
| 2026-11-11 | 84 211 |
| 2026-11-12 | 71 605 |
| 2026-11-13 | 188 643 |
| 2026-11-14 | 72 655 |
| 2026-11-15 | 66 436 |
| 2026-11-16 | 60 256 |
| 2026-11-17 | 56 911 |
| 2026-11-18 | 43 502 |
| 2026-11-19 | 41 189 |
| 2026-11-20 | 80 442 |
| 2026-11-21 | 35 999 |
| 2026-11-22 | 33 440 |

Снижение скорости не означает восстановление. По мере роста таблицы один проход становится дороже,
поэтому сам дефект замедляет следующий проход.

#### Большинство аренд не запускает login

За 24 часа пул `identity-hub:login` создал:

- 24 156 событий `lease.acquired`;
- 24 134 событий `lease.released`;
- 23 события `lease.expired`.

За то же окно зарегистрировано около 1,5 тыс. `identity.login.failed`. Значит, подавляющее
большинство login lease не дошло до `Authenticator.login()`.

Это соответствует коду `_run_logins()`: аренда создаётся на строке 749, а
`credential_candidate_state()` проверяется только на строках 753-760. Для 75 identity с
`credential_candidate_state() == False` цикл создаёт и сразу освобождает аренду, не увеличивая
`attempts`.

#### Реальные повторы тоже не имеют backoff

В production за 24 часа наблюдались повторяемые ошибки:

| Код | События | Identity |
| --- | ---: | ---: |
| `SignInRefused` | 1 535 | 6 |
| `TransportError` | 19 | 6 |
| `LoginAbandoned` | 2 | 2 |

`delivery-mail` преобразует только `InvalidCredentials` в
`LoginFailure(credentials_rejected, retryable=False)`. Неожиданный `SignInRefused` попадает в
общую ветку `except Exception`, возвращает identity в `no_session` и не задаёт долговечную задержку.
Распознавание конкретного отказа относится к provider adapter. Защита от частых повторов нужна
также для нераспознанных ошибок.

### Как возникает одна бесполезная строка

Текущая последовательность `auth.login_many()`:

1. `login_many()` строит query для `no_session` и `session_invalid`.
2. `pool.acquire()` выбирает identity и создаёт `ih_leases` и `ih_lease_history`.
3. `pool.acquire()` видит `Service.throttles` и вызывает `with_throttles()`.
4. `with_throttles()` читает `ih_throttle_state` и выполняет `last_unclean_lease_end()`.
5. Только после возврата аренды `_run_logins()` читает credential metadata и auth region.
6. Если кандидата credentials нет или retry запрещён, код освобождает аренду без входа.
7. Освобождение закрывает history и создаёт `lease.released`.
8. Следующий запуск `login_many()` повторяет тот же цикл.

По конфигурации `delivery-mail` контур ждёт 5 секунд между проходами с `limit=100`. При этом
неподходящие identity не увеличивают `attempts`, поэтому `limit` не ограничивает churn.

### Для чего нужен `last_unclean_lease_end()`

Запрос реализует требование N4.2 для эксклюзивного GCRA throttle.

При чистом завершении аренды `Lease._release()` сохраняет текущий TAT в `ih_throttle_state`. Если
процесс вызвал `Lease.throttle()`, а затем упал до release, последний TAT остаётся только в памяти
погибшего процесса. Следующий держатель использует время последнего `expired`, `lost` или `revoked`
lease как консервативную нижнюю границу:

```python
bound = unclean_released_at + throttle.interval * throttle.burst
tat = max(persisted_tat, bound)
```

Это предотвращает слишком ранний вызов провайдера после падения держателя. Тест
`test_expired_holder_recovers_conservative_bound` фиксирует данную гарантию.

Запрос нужен при текущей модели только в одном случае: эксклюзивная аренда впервые собирается
вызвать `Lease.throttle()` после возможного нечистого завершения предыдущего держателя.

| Контекст | Нужен recovery |
| --- | --- |
| Эксклюзивная send lease, затем `lease.throttle("send_message")` | Да |
| Чистая смена держателя | Нужен сохранённый TAT, history fallback обычно не влияет |
| Shared lease | Нет, используется атомарный `ih_throttle_state` на каждый вызов |
| `identity-hub:login`, который не вызывает `Lease.throttle()` | Нет |
| Probe, health или test без вызова throttle | Нет |
| Сервис без throttles | Нет, этот случай уже пропускается |
| Нечистая аренда старше максимального recovery window | Нет, bound уже в прошлом |

Следовательно, гарантию N4.2 удалять нельзя. Её текущую eager-реализацию через полный audit-журнал
нужно заменить.

### Почему текущая модель восстановления некорректна архитектурно

`ih_lease_history` одновременно используется как:

- техническая строка жизненного цикла аренды;
- операторский audit-журнал;
- источник `lease_history()` и `lease_stats()`;
- источник admin/read и части аналитики utilization;
- источник корректности GCRA после аварии.

Последний пункт смешивает audit retention с correctness state. По умолчанию закрытая история
хранится 90 дней. Если приложение настроит throttle window больше retention, удаление старой
нечистой аренды может ослабить лимит. Для типичного 20-секундного throttle это не проявляется как
ошибка корректности, но заставляет каждый раз читать данные, которые давно не могут повлиять на
результат.

Correctness state должен быть компактным и жить независимо от срока хранения операторской
истории.

### План выполнения проблемного запроса

Проблемный SQL:

```sql
SELECT released_at
FROM identity.ih_lease_history
WHERE space_id = $1
  AND identity_id = $2
  AND end_reason IN ($4, $5, $6)
  AND released_at IS NOT NULL
ORDER BY released_at DESC
LIMIT $3;
```

#### Custom plan с literal values

Для identity с 8 533 строками истории и одной `expired` записью PostgreSQL выбрал bitmap scan по
`ix_ih_lease_history_identity`:

```text
Bitmap Heap Scan on ih_lease_history
  Recheck Cond: identity_id = ...
  Filter: space_id = ... AND end_reason = ANY (...)
  Rows Removed by Filter: 8532
  Heap Blocks: exact=7419
  Buffers: shared hit=2868 read=4654
Execution Time: 270.929 ms
```

Даже custom plan читает историю identity целиком, чтобы найти одну редкую нечистую аренду.

#### Generic prepared plan без нечистых аренд

Production использует параметризованный запрос. Для identity без `expired/lost/revoked`
`force_generic_plan` выбрал обратный scan по `(space_id, released_at)`:

```text
Index Scan Backward using ix_ih_lease_history_space_released
  Index Cond: space_id = $1 AND released_at IS NOT NULL
  Filter: identity_id = $2 AND end_reason = ANY (...)
  Rows Removed by Filter: 1196345
  Buffers: shared hit=1082225 read=8453
  I/O Timings: shared read=234.698 ms write=8.948 ms
Execution Time: 2209.271 ms
```

Один отрицательный lookup отфильтровал около 1,196 млн строк, зарегистрировал около 1,09 млн
обращений к буферам (`shared hit + read`, включая повторные) и занял 2,2 секунды.

Статистика индекса подтверждает систематичность плана:

| Индекс | Размер | Scans | Index tuples read |
| --- | ---: | ---: | ---: |
| `ix_ih_lease_history_space_released` | 87 MiB | 801 650 | 478 163 991 276 |
| `ix_ih_lease_history_identity` | 106 MiB | 392 438 | 3 842 992 403 |
| `ix_ih_lease_history_space_holder_acquired` | 141 MiB | 0 | 0 |

`space_released` в среднем прочитал около 596 тыс. index tuples за один scan. Это основная причина
CPU, buffer churn и физического I/O PostgreSQL.

### Почему одного индекса недостаточно

Индекс вида `(space_id, identity_id, end_reason, released_at DESC)` резко удешевит lookup, но не
устранит:

- создание десятков тысяч бесполезных lease rows в сутки;
- insert и update каждой строки history;
- два lease event на каждую пустую аренду;
- рост семи индексов history и индексов events;
- отсутствие backoff внешней авторизации;
- зависимость correctness state от audit retention.

Индекс также уберёт случайный тормоз login loop. При 86 identity, интервале 5 секунд и быстром
проходе теоретическая верхняя граница составляет около 1,49 млн служебных аренд в сутки. Поэтому
индекс нельзя выкатывать отдельно от eligibility и retry fixes.

### Предлагаемое исправление

#### P0. Отбирать login candidates до аренды

Расширить store/query contract так, чтобы batch login выбирал только identity, для которых вход
может начаться сейчас.

Условие должно сохранить текущую семантику:

- credentials отсутствуют: `None`, Authenticator может получить данные другим способом, вход
  разрешён;
- существует active, не expired и не rejected credential candidate: вход разрешён;
- credentials существуют, но candidate нет: batch login пропускает identity до изменения
  credentials;
- `auth.retryable is False`: batch login пропускает identity, кроме случая появления новой
  credential candidate;
- `retry_at > now()`: batch login пропускает identity.

Проверка после `pool.acquire()` не подходит. Eligibility должна участвовать в candidate query или
в отдельном атомарном `pick_login_candidate`, чтобы гонка с заменой credentials оставалась
безопасной.

`limit` должен ограничивать число identity, для которых была создана аренда. Пропуск после гонки
тоже должен расходовать bounded inspection budget, иначе один вызов снова сможет просканировать
неограниченный хвост.

Явный `space.auth.login(identity_id)` может сохранить отдельную operator semantics и обходить
автоматический `retry_at`, если это документировано.

#### P0. Добавить долговечный retry schedule

Одного `retryable: bool` недостаточно. Для retryable login failure нужен `retry_at` или
эквивалентный fault deadline.

Минимальный контракт:

```python
class LoginFailure(...):
    retryable: bool
    retry_after: timedelta | None
```

Если provider не задал `retry_after`, Core применяет ограниченный exponential backoff с jitter.
Состояние хранится долговечно и участвует в batch candidate query. Успех, новая credential revision
или явная operator-команда сбрасывают backoff.

Неожиданное исключение Authenticator должно оставаться retryable, но не должно означать
немедленный бесконечный повтор.

#### P0. Загружать throttle state лениво

Удалить eager-вызов `with_throttles()` из `pool.acquire()`. Для exclusive lease выполнить
инициализацию один раз под per-name или общим throttle lock при первом `Lease.throttle(name)`.

Ожидаемый fast path:

```python
lease = await pool.acquire()
# Нет чтения ih_throttle_state и lease history.

delay = await lease.throttle("send_message", wait=False)
# Первое обращение загружает recovery state.
```

Для `SHARED` сохраняется существующий атомарный путь через БД на каждый вызов.

#### P1. Материализовать unclean recovery state

Не использовать `ih_lease_history` как источник correctness state. Хранить для identity компактный
монотонный marker, например `last_unclean_lease_end_at`.

Marker обновляется атомарно через `greatest(current, ended_at)` во всех путях закрытия аренды с
причиной:

- `expired`;
- `lost`;
- `revoked`.

Пути включают reclaim при следующем acquire, maintenance expiration, revoke и release с
соответствующей причиной. Marker можно хранить в отдельной throttle recovery table или рядом с
`ih_throttle_state`. Он не должен очищаться вместе с 90-дневной lease history.

При первом exclusive `Lease.throttle(name)` Core читает:

1. сохранённый TAT конкретного throttle;
2. `last_unclean_lease_end_at`;
3. вычисляет прежний `max(saved_tat, unclean + interval * burst)`.

Миграция backfill вычисляет marker как `max(released_at)` для
`end_reason IN ('expired', 'lost', 'revoked')`. После backfill `last_unclean_lease_end()` и его
history scan удаляются из store contract.

#### Временный containment, если materialized state откладывается

Временное решение должно включать все пункты ниже, а не один индекс:

1. Исправить eligibility и backoff.
2. Передавать в `last_unclean_lease_end()` нижнюю границу времени:

   ```python
   since = now - max(throttle.interval * throttle.burst for throttle in service.throttles.values())
   ```

3. Добавить индекс, соответствующий bounded query и prepared plan.
4. Добавить PG-тест с `plan_cache_mode=force_generic_plan` и отрицательным результатом.

Подходящий индекс нужно выбрать по реальному SQL. Partial index по literal причинам будет полезен
только если запрос не передаёт причины как неизвестные bind parameters. Индекс
`(space_id, identity_id, end_reason, released_at DESC)` устойчив к generic plan, но увеличивает
write amplification. Он остаётся временным решением, если materialized marker устраняет запрос
полностью.

### Политика хранения истории

`ih_lease_history` является журналом операций, а не таблицей «одна строка на аккаунт». Миллионы
строк допустимы только при миллионах значимых аренд.

После устранения no-op login leases сохранить действующий 90-дневный retention для реальных
аренд можно. Перед изменением срока нужно зафиксировать потребителей:

- операторская карточка `lease_history()`;
- `lease_stats()`;
- фильтры по последней аренде;
- admin/read lease pages;
- bounded utilization reports.

Долгосрочная аналитика уже должна опираться на rollups и события. Для внутренних пулов можно
добавить более короткий retention по `purpose` или классу пула, но это отдельное решение. Оно не
заменяет предотвращение no-op аренд.

События хранятся 180 дней, поэтому исправление только retention history оставит большую часть
storage amplification в `ih_events`.

### Миграция production-данных

Очистку выполнять только после выпуска логического исправления, иначе churn немедленно заполнит
таблицы снова.

Рекомендуемый порядок:

1. Выложить login eligibility и retry backoff.
2. Выложить lazy throttle recovery и materialized marker.
3. Выполнить backfill marker до удаления history.
4. Проверить отсутствие новых no-op `identity-hub:login` leases.
5. Удалять старые clean login rows небольшими commit batches.
6. Удалять соответствующие избыточные lease events только по явно утверждённой audit policy.
7. После массового delete дождаться autovacuum. `VACUUM FULL` на production не использовать без
   отдельного окна обслуживания. Для возврата диска рассмотреть `pg_repack` или online rebuild.

Открытые history rows, live leases, unclean rows и строки, нужные для backfill, удалять нельзя.

### Наблюдаемость

Добавить метрики, которые обнаруживают повтор проблемы до роста таблицы:

- `identity_hub_login_candidates_total{outcome=attempted|no_credentials|non_retryable|backoff}`;
- `identity_hub_login_lease_total`;
- `identity_hub_throttle_recovery_total{source=persisted|unclean_marker}`;
- `identity_hub_lease_history_rows` или документированный SQL/operator alert;
- длительность одного `login_many()` и число inspected identity;
- отношение `lease.acquired` к фактическим provider operations.

Alert должен срабатывать, если внутренний login-пул создаёт аренды без соответствующих
`identity.login.*` outcomes.

### Критерии готовности из исходного предложения

- [ ] 75 identity с rejected/disabled credentials не создают lease, history или events при
      `login_many()`.
- [ ] `login_many(limit=N)` создаёт не больше N lease даже при гонках и неподходящих candidates.
- [ ] Retryable failure получает долговечный `retry_at`; повтор до срока не создаёт lease.
- [ ] Новая credential revision сбрасывает non-retryable/backoff gate согласно документированному
      контракту.
- [ ] Сервис с throttles может acquire/release lease без чтения throttle recovery, если
      `Lease.throttle()` не вызывался.
- [ ] Первый `Lease.throttle()` после чистой смены держателя использует сохранённый TAT.
- [ ] Первый `Lease.throttle()` после expired/lost/revoked держателя сохраняет существующую
      консервативную гарантию.
- [ ] Удаление audit history по retention не меняет результат throttle recovery.
- [ ] MemoryStore и PostgreSQL дают одинаковое поведение.
- [ ] PostgreSQL-тест включает 1 млн clean history rows и отрицательный recovery lookup. Число
      прочитанных rows/pages не растёт линейно от размера history.
- [ ] PG-тест проверяет generic prepared plan, а не только literal/custom plan.
- [ ] Документация объясняет retry schedule, lazy throttle initialization и границу между
      correctness state и audit history.
- [ ] Upgrade guide описывает backfill marker и безопасную batch-очистку старых login rows.

### Тестовая матрица

| Сценарий | Ожидаемый результат |
| --- | --- |
| Нет credentials records и нет запрета на повтор | Batch login берёт штатную аренду и может вызвать Authenticator |
| Все credentials rejected | Batch login пропускает identity без lease |
| Новая active credential после reject | Identity снова становится candidate |
| `retryable=False` без новой credential | Автоматический вход не повторяется |
| Generic exception Authenticator | Retry назначается в будущем |
| Явный operator login | Поведение bypass определено и протестировано |
| Acquire/release без throttle | Нет throttle state/history recovery read |
| Первый throttle после clean release | TAT переносится |
| Первый throttle после process death | Консервативная задержка сохраняется |
| Audit history удалена | Throttle recovery не меняется |
| Shared throttle | Сохраняется атомарный DB path |
| 1 млн clean history rows | Отрицательный recovery не сканирует history |

### Затронутый код

- `src/identity_hub/auth.py::_run_logins`: eligibility проверяется после `pool.acquire()`;
- `src/identity_hub/lease.py::acquire`: eager `with_throttles()`;
- `src/identity_hub/lease.py::Lease.throttle`: целевая точка lazy initialization;
- `src/identity_hub/postgres/store.py::last_unclean_lease_end`: линейный history lookup;
- `src/identity_hub/testing/memory.py::last_unclean_lease_end`: MemoryStore parity;
- `src/identity_hub/store/base.py::StoreTx`: текущий recovery contract;
- `src/identity_hub/maintenance.py::_daily`: cleanup 90-дневной history;
- `src/identity_hub/hub.py::Retention`: `lease_history=90d`, `events=180d`;
- `tests/conformance/test_throttle.py`: действующий N4.2 invariant;
- `tests/conformance/test_import_acceptance.py`: retryable/non-retryable login semantics;
- PostgreSQL migrations: новый compact recovery state и backfill.

### Команды диагностики

Следующие запросы не содержат account identifiers и подходят для повторной проверки:

```sql
SELECT pool, end_reason, count(*) AS leases, count(DISTINCT identity_id) AS identities
FROM identity.ih_lease_history
WHERE acquired_at >= now() - interval '24 hours'
GROUP BY pool, end_reason
ORDER BY leases DESC;
```

```sql
SELECT indexrelname,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size,
       idx_scan,
       idx_tup_read,
       idx_tup_fetch
FROM pg_stat_user_indexes
WHERE schemaname = 'identity'
  AND relname = 'ih_lease_history'
ORDER BY pg_relation_size(indexrelid) DESC;
```

Для проверки worst-case плана использовать отдельную тестовую БД и identity без unclean history:

```sql
SET plan_cache_mode = force_generic_plan;
PREPARE last_unclean(uuid, uuid, int, varchar, varchar, varchar) AS
SELECT released_at
FROM identity.ih_lease_history
WHERE space_id = $1
  AND identity_id = $2
  AND end_reason IN ($4, $5, $6)
  AND released_at IS NOT NULL
ORDER BY released_at DESC
LIMIT $3;

EXPLAIN (ANALYZE, BUFFERS, SETTINGS)
EXECUTE last_unclean(:space_id, :identity_id, 1, 'expired', 'lost', 'revoked');
```

### Не цели

- Удаление lease history как продукта целиком.
- Ослабление fencing, exclusive lease или GCRA guarantees.
- Хранение provider credentials или ошибок в метриках.
- Массовая production-очистка в рамках изменения библиотеки.
- Маскировка provider adapter ошибки `SignInRefused`; адаптер должен классифицировать её отдельно.
