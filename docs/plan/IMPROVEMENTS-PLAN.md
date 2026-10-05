# Identity Hub — план имплементации улучшений

Источник требований: [`IMPROVEMENTS.md`](IMPROVEMENTS.md), требования v1:
[`identity-hub-requirements.md`](../../identity-hub-requirements.md), открытый долг:
[`DECISIONS.md`](DECISIONS.md) (`D-127`, `PERF-1`).
Прогресс: [`IMPROVEMENTS-PROGRESS.md`](IMPROVEMENTS-PROGRESS.md).
Протокол автономной работы: [`CLAUDE.md`](../../CLAUDE.md).

План предназначен для автономного исполнителя, который начинает с текущего состояния
`1.0.0rc1`, а не с исходного состояния до P0.1. Активный scope: `IMP-01`–`IMP-21`, `IMP-23` (фаза I8, `D-141`) и
закрытие `PERF-1`. `IMP-22 RuntimeProfile` не входит в эту очередь: он удалён из сводки
`IMPROVEMENTS.md` и явно отнесён к post-v1. Для него нужен отдельный запрос владельца и план.

По `D-142` исходное состояние, названное здесь `1.0.0rc1`, — выпущенный `1.0.0a1` с head
миграций `0007`; неизменяемы миграции `0001`–`0007`.

Этот файл — спецификация задач. Статусы ведутся только в `IMPROVEMENTS-PROGRESS.md`.
Изменение scope, разбиение или отмена задачи оформляются новой записью `D-NNN` в
`DECISIONS.md`, затем синхронно отражаются в плане и прогрессе.

---

## 0. Обязательные правила реализации

1. **Совместимость миграций.** Миграции `0001`–`0007` уже входят в `1.0.0a1` и не
   переписываются (`D-142`). Каждое изменение схемы получает новую последовательную миграцию. Backfill
   выполняется детерминированно; неоднозначные данные приводят к диагностируемой ошибке, а не к
   тихой потере или слиянию.
2. **Три реализации контракта.** Изменение Store проходит через `Store`/DTO, `PgStore` и
   `MemoryStore`. SQL остаётся внутри `identity_hub.postgres`; исключение — публичные read-only
   SQLAlchemy Core selectable из IMP-12.
3. **Транзакции.** Сетевой I/O не выполняется под транзакцией или блокировкой БД. Время TTL,
   claims, окон и retention берётся из PostgreSQL `now()`; monotonic используется только для
   локального самоограждения.
4. **Совместимость API.** Ломающие изменения публичного API требуют решения `D-NNN`, записи в
   CHANGELOG и migration guide. Где безопасно, старый вызов сохраняется на один RC с явным
   deprecation path.
5. **Секреты.** Plaintext, ключи, HMAC-теги и lease token не попадают в исключения, события,
   логи, repr, JSON или отчёты тестов. Любой новый секрет покрывается ротацией ключа и
   `reveal_secret`-аудитом.
6. **Конкурентность.** Race-тесты используют отдельные соединения и barrier/advisory lock только
   как тестовый барьер. `sleep` не используется для задания порядка событий.
7. **Трассировка.** Каждая задача обновляет матрицу `IMP → задача → тест`. Один IMP может
   закрываться несколькими задачами, но `done` допустим только после закрытия всех его критериев.
8. **Коммит задачи.** Код, тесты, документация и финальное состояние прогресса входят в один
   коммит `feat(Ix.y): ...`. Старт задачи фиксируется отдельным `plan: start Ix.y`.

### Проверки для каждой задачи

Основной gate:

```bash
bash scripts/check.sh
```

Для затронутой области до полного gate запускается узкий TDD-набор. Дополнительно:

- миграция: upgrade с `0007` и с предыдущей head, проверка metadata drift; downgrade, если он
  не разрушает пользовательские данные;
- конкурентность/heartbeat: полный релевантный набор 3 раза;
- производительность: smoke-набор на каждой задаче и полный load-run только в задачах с таким DoD;
- документация: проверка ссылок, команд и соответствия публичному API.

`xfail`, ослабление budget или удаление регрессии не считаются исправлением. Если локальное
окружение объективно не позволяет получить эталонный performance-результат, задача получает
`blocked`, сохраняет сырые результаты и автономный исполнитель продолжает другую доступную ветку.

---

## Фаза I0 — Baseline, безопасные контракты и эксплуатация

### I0.1 Зафиксировать scope и воспроизводимый baseline

- Записать новое решение `D-NNN`: приняты `IMP-01`–`IMP-21`; `IMP-22` отложен; старые миграции
  неизменяемы; целевая RC-версия выбирается до финального аудита по SemVer.
- Создать `docs/improvements-traceability.md` с 22 строками IMP (`IMP-01`–`IMP-21`, `IMP-23`), задачами этого плана, текущим
  состоянием (`missing|partial|implemented`) и будущими pytest node id.
- Зафиксировать commit, Python/PostgreSQL/CPU/pool и текущие результаты обычного gate и
  load-smoke. Сослаться на существующий `D-127`, не выдавать старые цифры за новый замер.
- Добавить документационный тест: `IMP-01`–`IMP-21` и `IMP-23` встречаются в матрице ровно по одному разу,
  все ID задач существуют в плане и прогрессе, `IMP-22` помечен `deferred`.
- **DoD:** `scripts/check.sh` зелёный; baseline и границы scope можно восстановить без устного
  контекста.

### I0.2a Стабильная модель ошибок и отображение в HTTP/CLI

Связанное улучшение: `IMP-13`. Разбиение I0.2 — `D-146`.

- Дать каждому публичному `IdentityHubError` стабильный `code` и безопасный `context`; добавить
  недостающие `RevisionConflict`, `IdempotencyConflict`, `LeaseOwnershipLost`, `LeaseRevoked`,
  `LeaseExpired`, `DatabaseBusy`, `SchemaIncompatible`, `SecretConfigurationError`.
- Вынести отображение в HTTP status и CLI exit code в чистые функции без зависимости ядра от
  Litestar. Существующие типы сохранять алиасами/наследниками там, где это не меняет семантику.
- **DoD:** unit-тесты кодов (уникальны, стабильны, есть у каждого публичного класса), безопасной
  сериализации `context` (без секретов и сырых значений) и mappings; CLI возвращает exit code по
  коду ошибки.

### I0.2b Порядок блокировок и архитектурные границы

Связанные улучшения: `IMP-14`, `IMP-19`. Разбиение I0.2 — `D-146`.

- Документировать и реализовать единый порядок блокировок из IMP-14. Многострочные блокировки
  сортируют identity UUID; dispatcher блокирует только свои очереди.
- Подключить import-linter и архитектурные тесты: domain/core не импортируют SQLAlchemy, Alembic,
  httpx или aiogram; интеграции не импортируют приватный `postgres.store`; публичный `__all__`
  остаётся явным.
- **DoD:** pg race-тест обратного порядка; import contracts входят в `scripts/check.sh`.

### I0.3 Лимиты payload, versioned envelope и `SecretValue`

Связанное улучшение: `IMP-16`.

- Ввести общие валидаторы: JSON до 64 KiB и глубины 16, plaintext секрета до 512 KiB,
  сериализованный envelope до 1 MiB. Применить на всех входах, включая import, events,
  identity_data, profile, webhook и Telegram settings.
- Добавить `schema_version` в AAD формата `identity-hub:v1:...`. Расшифровка старого AAD из
  `1.0.0rc1` остаётся доступной; очередная запись/rotate-key переводит payload в новый формат.
- Реализовать `SecretValue` с редактированными `str`/`repr`, запретом JSON-сериализации и явным
  контролируемым раскрытием.
- Проверить отказ до открытия транзакции для oversized input и отсутствие plaintext во всех
  сообщениях об ошибках.
- **DoD:** boundary/property-тесты размеров и глубины; roundtrip legacy/new envelope; rotate-key
  обновляет AAD; scrubber отвергает `SecretValue`.

### I0.4 Проверка схемы при старте и lock миграции

Связанное улучшение: `IMP-15`.

- Добавить в Store capability чтение текущей `ih_alembic_version`; `IdentityHub.start()` для
  PostgreSQL требует ровно поддерживаемую head. `MemoryStore` сообщает совместимую in-memory
  ревизию без Alembic.
- Более старая, более новая, отсутствующая или разветвлённая ревизия приводит к
  `SchemaIncompatible` с ожидаемой/фактической ревизией, без URL и SQL-параметров.
- `identity-hub migrate` берёт PostgreSQL advisory lock на namespace Identity Hub на весь upgrade
  и гарантированно освобождает его. Автомиграции в `hub.start()` нет.
- Обновить CLI `doctor` и документацию запуска.
- **DoD:** pg-тесты old/new/missing/head и двух конкурентных migrate; тест, что обычная работа не
  использует advisory lock; CLI exit code соответствует I0.2.

---

## Фаза I1 — Идентичность, revision и идемпотентность

### I1.1 Нормализация внешнего идентификатора

Связанное улучшение: `IMP-08`.

- Расширить `Service` полями `normalize_ref` (по умолчанию `str.strip`) и
  `normalize_ref_version`; проверить детерминированность и непустой результат.
- Новой миграцией добавить `normalized_ref` и версию нормализации, выполнить backfill, затем
  заменить уникальность на `(service, normalized_ref)`. Коллизии вывести списком безопасных ID и
  остановить миграцию без изменения данных.
- Выполнять нормализацию до lookup, register, import, link resolution, search exact-match и
  fingerprint. `external_ref` хранит исходное значение для отображения.
- Обновить MemoryStore, фабрики и документацию смены версии нормализатора.
- **DoD:** email/phone/custom normalizer; дубль внутри пространства и между пространствами;
  import-report после нормализации; migration collision test; conformance parity.

### I1.2a Revision агрегата: колонка, рост, события, read-схема

Связанное улучшение: `IMP-07`. Задача `I1.2` разбита решением `D-205` на `I1.2a` и `I1.2b`.

- Новой миграцией добавить `ih_identities.revision BIGINT NOT NULL DEFAULT 1`; определить полный
  список изменений агрегата, каждое из которых увеличивает revision ровно один раз на команду.
- Добавить revision в события и read DTO. Region/data version остаются локальными CAS и не
  подменяют общую revision.
- Синхронизировать PgStore/MemoryStore; повтор идемпотентной команды не увеличивает revision.
- `StoreTx.lock_identity_revision` сверяет новую колонку и при расхождении даёт
  `RevisionConflict`; `doctor` проверяет нижнюю границу по ревизии последнего события.
- **Дополнение из ревью (`D-151`).** `R2-D23`: `hub.read.identities.c.revision` переводится на новую колонку, `READ_SCHEMA_VERSION`
  повышается. Регрессионный тест `F2.4` (параллельные `add_tags`) остаётся зелёным.
- **DoD:** рост ровно на 1 для каждой команды изменения, без роста от аренды и раскрытия секрета;
  все события команды несут одну ревизию; миграция с backfill и downgrade; read-схема v2;
  conformance memory/pg.

### I1.2b `expected_revision` в командах изменения

Связанное улучшение: `IMP-07`.

- Добавить `expected_revision` к операторским и `using(session)` mutation API. `None` сохраняет
  совместимость, переданное значение проверяется атомарно и при конфликте даёт
  `RevisionConflict`.
- Перевести админку на проверку ревизии внутри команды вместо отдельной транзакции.
- **DoD:** две admin-сессии с одной revision, один победитель; rollback/savepoint; revision в
  последовательности событий; conformance для всех mutation families; документация и CHANGELOG.

### I1.3 Единый протокол идемпотентности и canonical fingerprint

Связанное улучшение: `IMP-03`.

- Мигрировать `ih_operation_keys` к PK `(namespace, key)` и полям operation, fingerprint version,
  fingerprint, result type/version/data, owner identity и expires_at. Существующие записи
  конвертировать в legacy namespace.
- Реализовать canonical encoder: отсортированные ключи, нормализованные refs, UUID, UTC, различие
  omitted/null, schema/version. Сырые секреты никогда не входят в canonical JSON.
- Реализовать transaction primitive claim/complete/replay/conflict: незакоммиченный победитель
  естественно удерживает конкурента; rollback освобождает ключ; наружного `pending` нет.
- Перевести acquire, operation и новые idempotency key для register/import на общий primitive;
  результат replay типизирован и версионирован.
- **Выполняется досрочно** задачами `F1.1` и `F1.2` из `RELEASE-FIXES-PLAN.md` (`D-151`).
- **DoD:** barrier race winner/replay, same key/different body, rollback winner, namespace
  isolation, retention; одинаковое поведение memory/pg.

### I1.4a Кольцо ключей отпечатков и HMAC-отпечаток прокси

Связанное улучшение: `IMP-04`. Задача `I1.4` разбита решением `D-207` на `I1.4a` и `I1.4b`.

- Расширить SecretsProvider отдельным fingerprint keyring с current и historical key ID; KEK
  шифрования не используется как HMAC-ключ. Без явного кольца ключи выводятся из KEK через
  HKDF с отдельной меткой.
- Примитив HMAC-SHA-256 equivalence tag; тег и ключи не попадают в repr, события и логи.
- **Дополнение из ревью (`D-151`).** `R2-S16`: fingerprint прокси считается HMAC из fingerprint keyring вместо
  SHA-256 без ключа; миграция добавляет key ID, пересчёт значений — командой обслуживания
  (у миграции нет ключей), до пересчёта прежние значения находятся при поиске.
- **DoD:** KEK ≠ HMAC key; одинаковый/другой логин; rotation overlap (дедупликация по старому
  ключу и legacy SHA-256); пересчёт; отсутствие ключа/тега в repr/log/event; conformance memory/pg.

### I1.4b HMAC-тег секретов в идемпотентных командах

Связанное улучшение: `IMP-04`.

- Для секретных полей canonical command хранит только HMAC-SHA-256 equivalence tag и key ID.
  Несекретная часть остаётся обычным стабильным hash.
- Интегрировать register/import, credentials/session update и другие идемпотентные команды с
  секретами. Ротация создаёт новые записи новым ключом, старые проверяются до expires_at.
- Отсутствующий historical key для живой записи даёт `SecretConfigurationError`, а не cache miss.
- **DoD:** одинаковый/другой secret, rotation overlap, missing key, отсутствие secret/tag в
  repr/log/event; conformance replay.

---

## Фаза I2 — Аренда и heartbeat

### I2.1 Атомарная LRU-выборка аренды

Связанное улучшение: `IMP-01`; закрывает lease-часть `PERF-1`.

- Новой миграцией добавить `ih_identities.last_leased_at` и частичный индекс
  `(space_id, service, last_leased_at ASC NULLS FIRST)` с точным predicate допустимых кандидатов.
- Заменить Python retry/candidate loop одним CTE: `FOR UPDATE OF identity SKIP LOCKED LIMIT 1`,
  атомарный increment fencing и `last_leased_at=clock_timestamp()`, затем создание аренды в той же
  транзакции. Конфликт уникальности остаётся внутренним retry с ограниченной причиной.
- Сохранить все фильтры `for_operation`, tags/labels/profile/among и proxy policy. MemoryStore
  выдаёт наименее недавно арендованный identity с детерминированным tie-breaker.
- **DoD:** EXPLAIN использует индекс; равномерная ротация; 0/50/90/99% занятости на 100k,
  p95 pick ≤ 5 ms и целевой lease budget ≤ 20 ms в эталонном окружении; нет двух живых аренд.

### I2.2a HOT heartbeat: индексы, fillfactor и редкая запись истории

Связанное улучшение: `IMP-02`. Задача `I2.2` разбита решением `D-210` на `I2.2a` и `I2.2b`.

- Новой миграцией удалить индекс `ih_leases.expires_at` и частичный
  `ix_ih_lease_history_open`, установить `fillfactor=70`; maintenance находит просроченные строки
  через leases/token без full history scan.
- История получает счётчики/last heartbeat периодически (default 5 min) и при close, а не на
  каждый heartbeat.
- **Дополнение из ревью (`D-151`).** `R2-D01`: после удаления индексов тест `F2.2` (брошенная аренда закрывается при
  600 освобождённых) остаётся зелёным.
- **DoD:** 10 циклов дают `n_tup_hot_upd / n_tup_upd ≥ 0.9`; release не теряет счётчики;
  список живых аренд показывает свежий heartbeat; lifecycle-тесты проходят 3 раза.

### I2.2b Пакетный heartbeat и координатор процесса

Связанное улучшение: `IMP-02`.

- Добавить Store API batch renew одним `UPDATE ... WHERE token = ANY(...) RETURNING`; процессный
  heartbeat coordinator группирует активные leases, но создаётся только при явном использовании
  lease и завершается после последней аренды.
- Потеря отдельной аренды в batch ограждает только её владельца.
- **DoD:** batch 100/500 имеет benchmark; shutdown не теряет close/counters; lifecycle-тесты
  проходят 3 раза.

### I2.3 Полный lease fence

Связанное улучшение: `IMP-05`.

- Ввести публично безопасный `LeaseFence(identity_id, lease_id, token, fencing)`; token исключить
  из repr. Внутренний guard проверяет равенство всего кортежа, живую аренду и точное равенство
  `ih_identities.fencing_seq`.
- Передавать fence во все записи от воркера: session/credentials, operation result, verification,
  provider-operation apply, cool_down и закрытие lease history.
- Удалить проверки вида `fencing >= current` или проверку только sequence. PgStore блокирует
  identity до применения; MemoryStore повторяет ту же семантику.
- **Дополнение из ревью (`D-151`).** `R1-15`: `operation_scope` проверяет fence под блокировкой строки и живую
  аренду. `R2-S05`: чтение credentials и session проверяет fence в транзакции чтения.
- **DoD:** подмена каждого поля, expired/reacquired/revoked lease и zombie write дают
  `LeaseOwnershipLost`; валидный fence атомарно пишет данные и событие.

### I2.4 Семантика renew, release и revoke

Связанное улучшение: `IMP-06`.

- `renew` задаёт `expires_at=max(old_expires_at, now()+ttl)` и никогда не укорачивает аренду.
- Свой live release закрывает lease/history; повтор и release естественно истёкшей аренды —
  успешный no-op; release отозванной аренды даёт `LeaseRevoked`; потеря ownership отделена от
  expiry.
- `revoke` фиксирует actor/reason, закрывает историю, увеличивает fencing и немедленно будит
  heartbeat coordinator. Повторный revoke детерминирован.
- **Дополнение из ревью (`D-151`).** `R1-16`: в таблицу сценариев входит «тело блока завершилось успешно, аренда
  потеряна по пути»; исход (`LeaseLost` с признаком завершённого тела) описан в руководстве.
- **DoD:** таблица сценариев ошибок, renew clock skew, release/revoke races на barrier, события и
  history reason; pg/memory conformance.

---

## Фаза I3 — Credentials и sessions

### I3.1a Credentials: схема, виды в Service и контракт Store

Связанное улучшение: `IMP-09`. Задача `I3.1` разбита решением `D-214` на `I3.1a` и `I3.1b`.

- Новой миграцией преобразовать `ih_credentials`: UUIDv7 `id`, owner/space, kind, name,
  public_value, envelope, lifecycle, expires_at, revision; уникальность
  `(identity_id, kind, name)`. Legacy-строку перенести как default credential без потери envelope.
- `Service` объявляет credential kinds и Pydantic secret schema для каждого kind; неизвестная
  kind/name отвергается до транзакции.
- **Дополнение из ревью (`D-151`).** `R2-P08`: `MemoryStore` хранит `version` и `updated_at` credentials и сессий;
  conformance для `secrets.describe`.
- **DoD:** migration roundtrip legacy data; несколько однотипных ключей в Store; tenant isolation;
  conformance memory/pg.

### I3.1b Credentials: lifecycle, срок и команды

Связанное улучшение: `IMP-09`.

- Реализовать lifecycle `active ↔ disabled`, `revoked` terminal и ленивую проверку expires_at.
  Секрет, public_value и metadata изменяются атомарно с revision/event.
- **DoD:** expiry/disable/revoke; отсутствие секретов в read models и событиях; conformance.

### I3.2 API нескольких credentials и интеграция

Связанное улучшение: `IMP-09`.

- Добавить `lease.credential(kind, name="default")` и `lease.credentials()` для всех активных
  записей, защищённые полным fence. Зафиксировать совместимость старого default accessor решением
  I0.1 и migration guide.
- Обновить register, import, login, secret admin, rotate-key, identity card и testing factories.
  List/read API возвращают только id/kind/name/public metadata/lifecycle/revision.
- Идемпотентное обновление использует I1.3/I1.4b; reveal конкретного credential создаёт audit
  event с kind/name, но без значения.
- **DoD:** end-to-end register→lease→rotate→reveal, import replay, linked login и
  MemoryStore/PgStore conformance.

### I3.3 Два режима обновления session

Связанное улучшение: `IMP-10`.

- Зафиксировать lifecycle session `created → active → invalidated → revoked`, где revoked
  terminal; мигрировать legacy statuses без потери ciphertext.
- `save_session(mode="in_place")` обновляет активный slot и revision; `mode="replace"` создаёт
  новую запись и атомарно отзывает старую, сохраняя историю.
- Event data содержит slot/mode/old/new session id/revision без payload. Login/refresh явно
  выбирают режим по результату адаптера.
- **DoD:** обе ветки, concurrent replace, rollback, zombie fence, terminal revoke и чтение только
  новой active session; pg/memory parity.

---

## Фаза I4 — Долговечные внешние операции

### I4.1 State machine, хранение и claim provider operation

Связанное улучшение: `IMP-11`.

- Реализовать чистую state machine `pending → running → succeeded|retryable|failed`, cancel и
  recovery истёкшего claim. Определить retry/deadline policy и безопасные error codes.
- Новой миграцией создать `ih_provider_operations` с attempt, claim tuple, owner identity,
  available/deadline timestamps, безопасным result и revision. Индексы поддерживают claim без
  full scan.
- Store API делает короткие claim/apply/recover транзакции. Внешний адаптер вызывается между
  ними; apply принимает provider claim и lease fence, поздний ответ отвергается.
- **DoD:** state-table unit tests; два claim worker; expired claim recovery; late apply; crash
  between claim/call/apply; at-least-once задокументирован без заявления exactly-once.

### I4.2 Login, refresh и challenge поверх очереди операций

Связанное улучшение: `IMP-11`.

- `run_logins` планирует/claim-ит durable operation, коммитит до вызова Authenticator и передаёт
  стабильный attempt id как idempotency key, если адаптер его поддерживает.
- Success/session replace, retryable error, permanent error и LoginChallenge применяются второй
  транзакцией с обоими fences. Повтор процесса не создаёт второй открытый challenge.
- Сохранить публичный удобный метод, но вернуть безопасный report: planned/claimed/succeeded/
  retryable/failed; не скрывать незавершённые операции.
- **DoD:** kill/restart после каждого checkpoint, duplicate worker, challenge/answer и поздний
  результат старого воркера; ни одна транзакция не охватывает network call.

### I4.3 Probes, reconcile и recovery worker

Связанное улучшение: `IMP-11`.

- Перевести auth/standing/verification probe, resync и reconcile на ту же очередь с kind
  `probe:<region>`. Дедупликация не допускает параллельные живые операции одного kind/identity.
- Recovery переводит expired running в retryable/failed отдельным commit; следующий claim идёт
  обычным путём. Quota списывается по явно документированной точке и не дублируется при replay.
- Provider worker предоставляет `run_once(limit)` и счётчики для I5.2; maintenance вызывает
  recovery без сетевого I/O.
- **DoD:** pinned drift, probe failure/retry, process restart, deadline, quota replay и late apply;
  invariant no-running-with-expired-claim после recovery.

---

## Фаза I5 — Публичное чтение, workers и упорядоченная доставка

### I5.1 Публичная read-схема SQLAlchemy Core

Связанное улучшение: `IMP-12`.

- Добавить `hub.read` со стабильными typed Core selectable: identities, leases, lease_history,
  region_states, events, proxies. Физические ORM/table objects остаются приватными.
- В selectable нет ciphertext, wrapped DEK, nonce, token, fingerprint, claim token и внутренних
  lock fields. Версия read schema объявляется отдельно.
- Поддержать join с таблицами приложения, `among=Select`, `db_schema` через документированный
  `schema_translate_map` и aliases.
- **DoD:** compile/execute примера из IMP-12 в default/custom schema; allowlist колонок; public
  import contract; SQL snapshot не ссылается на секретные поля.

### I5.2 Единый `hub.workers.*.run_once(limit)`

Связанное улучшение: `IMP-20`.

- Ввести фасады `dispatch`, `maintenance`, `reconcile`, `provider_operations` с
  `run_once(limit)` и типизированными безопасными reports. `limit` обязателен к соблюдению на
  уровне SQL reservation/claim, а не после загрузки всех строк.
- Существующие `dispatcher.run()`/`running()` и CLI становятся циклами поверх run_once с
  LISTEN/NOTIFY или polling; бизнес-ядро не дублируется.
- Импорт пакета, создание hub и `hub.start()` не запускают background thread/task. Lifecycle
  worker явно принадлежит вызывающему приложению и корректно завершается.
- **DoD:** limit 0/1/N, cancel/shutdown, два worker, пустая очередь, report serialization; тест
  отсутствия созданных asyncio tasks после import/init/start.

### I5.3 Опциональный ordered outbox

Связанное улучшение: `IMP-21`.

- Добавить endpoint policy `ordered=False` по умолчанию и resource key/revision для delivery.
  Миграция backfill сохраняет прежнюю неупорядоченную семантику.
- При `ordered=True` событие следующей revision одного identity/recipient не claim-ится, пока
  предыдущая delivery не terminal-success. Другие identity и endpoints не блокируются.
- Определить поведение dead/manual retry/retention, чтобы head-of-line block был наблюдаем и
  устраним оператором.
- **DoD:** out-of-order commit, retry/dead/manual retry, два identity и два endpoint; unordered
  throughput не регрессирует; EXPLAIN использует новый индекс.

---

## Фаза I6 — Performance и детерминированная проверка

### I6.1 Fast path операции

Закрывает operation-часть `PERF-1`.

- Снять профиль round trips успешной `lease.operation`: snapshot/fence, idempotency, quotas,
  pacing и event. Объединить совместимые SQL-шаги в одну transaction round trip без ослабления
  атомарности и списания квоты до platform call.
- Сохранить все fault branches, revision, full fence и MetricsHook. Оптимизация не переносит
  network call внутрь транзакции.
- **DoD:** operation p95 ≤ 10 ms в сценарии §10.1; quota/idempotency races зелёные; план и число
  SQL round trips зафиксированы регрессией.

### I6.2 План поиска под общей нагрузкой

Закрывает search-часть `PERF-1`.

- Собрать `EXPLAIN (ANALYZE, BUFFERS)` для free text, exact UUID/ref, labels/profile и keyset на
  100k при конкурентной lease/operation нагрузке.
- Исправить query/index/statistics так, чтобы planner использовал selective GIN/trigram path;
  не менять результат, escaping и ограниченный count.
- **Дополнение из ревью (`D-151`).** `R2-D08`: `account_search` без N+1 (пакетные запросы, тест на число
  запросов). `R2-D09`: список по умолчанию использует индекс (кортежное сравнение курсора,
  без `NULLS LAST` для `NOT NULL` колонок).
- **DoD:** search p95 ≤ 100 ms; планы ключевых форм закреплены тестами без хрупкой проверки
  стоимости; pagination/tenant isolation не регрессируют.

### I6.3 Set-based import fast path

Закрывает import-часть `PERF-1`.

- Подготовить/валидировать/зашифровать batch до транзакции; загрузить staging через COPY или
  эквивалентный set-based путь; одной серией SQL применить identity, credentials, sessions,
  links, proxies, verifications и events.
- Сохранить размер транзакции, `on_existing`, per-record report, idempotency/fingerprint и
  `on_batch_committed`. Ошибка batch не останавливает следующие batch.
- MemoryStore остаётся референсом семантики, но не имитирует COPY.
- **DoD:** import 10k ≤ 60 s; mixed/duplicate/link-within-batch/error batch parity; rollback не
  оставляет staging/user data.

### I6.4 Полный load harness и бюджеты

Связанное улучшение: `IMP-17`; финально закрывает `PERF-1`.

- Довести `benchmarks/seed.py`/`run.py`: отдельный COPY seed, warmup, concurrency
  1/4/8/16/32/64, p50/p95/p99, empty picks, error taxonomy и raw machine-readable output.
- После каждого прогона проверять: одна live lease на identity, live fencing равен sequence, нет
  leak pool connections/deadlock/stuck provider claims. Реализовать saturation rule из IMP-17.
- Создать датированный отчёт с commit, CPU, PostgreSQL, pool/settings и сравнением с D-127.
- **DoD:** все пять бюджетов §10.1 проходят без xfail; 4 старых xfail удалены только после
  зелёного полного прогона; `D-127` помечен закрытым новой записью, история не переписывается.

### I6.5 Полная deterministic race matrix

Связанное улучшение: `IMP-18`.

- Реализовать barrier-сценарии: два acquire последнего identity; acquire против release/renew;
  последняя quota; expiry против zombie; одинаковый idempotency key и rollback winner; override
  против observe; revoke против renew; delete против acquire.
- Добавить races revision, credential replace, session replace, provider late apply и ordered
  delivery. Каждый сценарий утверждает итоговые данные, events/history и тип проигравшей ошибки.
- Запустить матрицу минимум 20 повторов отдельной командой и основной pg-набор 3 раза; случайный
  `sleep` и повышение timeout не являются синхронизацией.
- **DoD:** нет flaky/xfail; invariant checker из I6.4 зелёный после каждого сценария.

---

## Фаза I7 — Документация и финальный аудит

### I7.1 Migration guide и документация новых контрактов

- Написать переход с `1.0.0rc1`: backup/preflight, upgrade, проверка normalized_ref collisions,
  новые fingerprint keys, multi-credentials/session API, schema check и rollback boundaries.
- Обновить README, CLI, integrations, error reference, workers lifecycle, public read schema,
  idempotency guarantees, lease semantics и performance reproduction.
- Завершить `docs/improvements-traceability.md`: каждый IMP-01–21 с фактическими pytest node id,
  миграцией/API и решением D-NNN. `IMP-22` остаётся явно deferred.
- **DoD:** документационные тесты, ссылки и copy-paste examples зелёные; ни одна гарантия не
  сильнее теста или реализации.

### I7.2 Финальный аудит и release candidate

- Проверить публичный `__all__`, стабильные error codes, read schema version, migration head,
  MemoryStore/PgStore conformance и отсутствие секретов во всех DTO/events/reports.
- Запустить `scripts/check.sh` 3 раза, полную deterministic race matrix и эталонный load-run.
  Матрица IMP должна быть без `missing|partial`, кроме отдельно deferred IMP-22.
- Выбрать следующую RC-версию согласно решению I0.1/SemVer, обновить package metadata, lock,
  CHANGELOG и migration guide одним коммитом. Публикация и push не выполняются.
- **DoD:** обычные тесты без deselected regressions, load budgets без xfail, schema upgrade с
  `0004`, чистый diff относительно файлов задачи и воспроизводимый финальный отчёт.

---

## Фаза I8 — Публичное API: неймспейсы и конвенция глаголов

Связанное улучшение: `IMP-23`, решение `D-141`. Фаза выполняется **сразу после I0.1**: строки
I8.1–I8.9 стоят в прогрессе перед I0.2, а I8.10 — перед I7.1.

Общие правила фазы:

- Фасады — тонкие классы в `identity_hub/api/` (`identities.py`, `imports.py`, …). Они держат
  ссылку на `SpaceHandle` и вызывают существующую логику миксинов. Бизнес-логика не
  дублируется, Store/SQL не меняются, кроме явно указанных мест. Миграций БД в фазе нет.
- `SpaceHandle` получает неймспейсы как `cached_property`. Каждый старый метод становится
  алиасом через общий хелпер `deprecated(old, new)`: `DeprecationWarning` с `stacklevel=2` и
  текстом «`space.X` устарел, используйте `space.Y`», затем вызов нового метода.
- Задачи I1–I7 после I8 добавляют новые параметры (`expected_revision`, idempotency key,
  нормализованный ref, несколько credentials) **только** в методы неймспейсов. Алиасы новых
  параметров не получают.
- Каждая задача дописывает свои строки в `docs/api-migration.md` и обновляет allowlist.

### I8.1 Каркас неймспейсов, allowlist поверхности и таблица миграции

- Создать пакет `identity_hub/api/` с базовым классом фасада и хелпером `deprecated`.
- Добавить тест-allowlist: публичные атрибуты `SpaceHandle` = неймспейсы + замороженный список
  текущих 89 имён (помечены `legacy`). Новое публичное имя вне неймспейса валит тест.
- Создать `docs/api-migration.md` с таблицей «старое → новое» для всех 89 имён (включая
  «переходит в `space.admin`» и «удаляется из публичного API»). Документационный тест
  сверяет таблицу с `dir(SpaceHandle)`.
- Добавить в `docs/guide/01-concepts.md` определение «identity, или аккаунт» и таблицу глаголов
  из IMP-23.
- **DoD:** allowlist-тест и тест таблицы зелёные; unit-тест хелпера (одно предупреждение,
  корректный stacklevel, результат идентичен).

### I8.2 `space.identities`: чтение

- Ввести DTO `Identity` (secret-free, `repr` без секретов) и `IdentityRef`.
- `get(id)` → `NotFound`; `find(id=None, *, service=None, external_ref=None)` → `None`;
  `get_by_ref(service, ref)`; `card(id)` (бывш. `identity_card`); `search(query, limit, after)`
  → `Page[Identity]` (бывш. `search`); `list(IdentityFilter, limit, after)` со
  структурированными полями (service, регионы, tags, labels, filterable_profile) поверх того же
  query-движка; `count(filter)`.
- Опираться на существующие `StoreTx.get_identity` / `find_identity`. Чтение из чужого
  пространства даёт `NotFound`.
- **DoD:** conformance MemoryStore/PgStore; get/find на отсутствующем и чужом id; list и search
  дают одинаковый набор на эквивалентных запросах; сериализация `Identity` без секретных полей.

### I8.3 `space.identities`: создание

- Модель `IdentityCreate` (поля из IMP-23); `create(spec)` → `Identity` или `AlreadyExists`;
  `ensure(spec)` → `(Identity, created)` без обновления существующего.
- `create_many(specs, *, on_conflict="fail"|"skip", actor)` → `CreateManyResult(created,
  skipped)`: валидация всей пачки до транзакции, одна транзакция, лимит 1000, блокировки в
  порядке сортировки `(service, external_ref)`.
- `register(...)` — алиас `ensure`, возвращающий прежний `RegisterResult`.
- **DoD:** атомарность (одна невалидная запись → ничего не создано); `fail`/`skip`; дубль
  внутри пачки; конфликт с другим пространством; pg-race двух пересекающихся `create_many` без
  deadlock; одно событие `identity.created` на identity; parity алиаса.

### I8.4 `space.identities`: изменение и состояние

- `update(id, *, profile=, labels=, note=, actor)` — только слияние (`None` в labels удаляет
  ключ); `replace_profile`, `replace_attributes`; `add_tags` / `remove_tags` / `set_tags`;
  `delete(id)`.
- Состояние: `activate` (→ `READY`), `retire`, `set_lifecycle`, `restore_standing`, `override`,
  `unpin` с прежними правилами переходов и причин.
- Алиасы: `set_labels`, `update_profile`, `set_attributes`, `set_note`, теги, `delete`,
  команды состояния.
- **DoD:** событие только при реальном изменении; `activate` из `provisioning` и `retired`;
  `TransitionNotAllowed` сохраняется; parity всех алиасов.

### I8.5 `space.imports` и `space.secrets`

- `ImportRecord` остаётся отдельным типом (`D-144`): общие поля совпадают с `IdentityCreate`,
  `links` — строковые ref, импорт всегда создаёт `provisioning`.
- `imports.run` (бывш. `import_many`, все параметры и `ImportReport` без изменений),
  `run_files`, `stage`, `get(run_id)`, `cancel(run_id)`.
- `secrets.describe(id)` (бывш. `access_card`), `secrets.reveal(id, SecretRef, *, actor,
  reason)`, `replace_credentials`, `replace_session(id, data, *, slot)`. Строковые ref
  принимаются алиасом.
- **DoD:** существующие import-тесты проходят через `imports.run` с теми же отчётами;
  stage/cancel; событие `identity.secret.revealed` не изменилось; секреты не попадают в тексты
  предупреждений.

### I8.6 `space.proxies`, `space.pools` и типизированная аренда

- `proxies`: `get`, `find(url)`, `list(group=, status=)`, `create(ProxySpec)`, `ensure`,
  `create_many(urls, *, group)`, `report_failure`, `report_success`,
  `recheck(id, checker, *, actor)`, `recheck_due(checker, *, limit)`, `override`, `rebind`.
- `group` вместо `pool` в публичных именах: `ProxySpec.group`, `ProxyPolicy.group`
  (`pool=` — с предупреждением). Колонка `ih_proxies.pool` не меняется.
- `pools.define(name, ...)` (бывш. `space.pool`), `pools.revoke(identity_id, ...)`,
  `Pool.capacity()` (бывш. `check_capacity`).
- `Pool.lease` / `Pool.acquire`: явная сигнатура вместо `**kwargs: Any`.
- **DoD:** неизвестный параметр аренды → `TypeError`; mypy по examples; прокси-тесты через
  `group`; parity `ProxyPolicy(pool=)` и `ProxyPolicy(group=)`.

### I8.7 `space.auth`, `space.sync`, `space.links`, `space.data`, `space.verifications`

- `auth.login(id)` → `LoginOutcome` (`StrEnum`, значения равны старым строкам), `login_many`,
  `answer_challenge`.
- `sync.refresh` (бывш. `resync`), `sync.refresh_many` (бывш. `reconcile`).
- `links.add`, `links.remove`, `links.of`, `links.impact`; `data.load`, `data.save`;
  `verifications.status`, `review`, `requirements.add`, `requirements.resolve`.
- **DoD:** `LoginOutcome` сравнивается со старыми строками; parity всех алиасов; сценарий
  onboarding из руководства на новых именах.

### I8.8a Неймспейсы admin, analytics и maintenance

Разбиение I8.8 — `D-147`.

- `space.admin`: read-модели UI (`overview`, `pool_summary`, `pool_capacity`, lease dashboard и
  history, events, deliveries, human queue, proxy overview/detail, `identity_page` — бывш.
  `account_search`, `selection_snapshot` — бывш. `account_selection_snapshot`, import runs,
  provider operations, `link_summary`) и аудит `record_*`.
- `space.analytics`: `series`, `funnel`, `time_to`, `cohorts`, `ban_rate`, `utilization`,
  `proxy_quality`.
- `space.maintenance` (класс `SpaceMaintenance`): `expire_challenges`, `expire_verifications`,
  `reindex_search`. Задача I5.2 позже оборачивает их в `hub.workers`.
- Старые имена — legacy-алиасы; внутренние вызовы библиотеки (диспетчер, обслуживание,
  read-модели) переведены на новые имена или приватные реализации.
- **DoD:** все 32 перенесённых метода доступны в неймспейсах с прежними сигнатурами; allowlist
  содержит только неймспейсы и legacy-алиасы; таблица миграции отмечена.

### I8.8b Перевод потребителей на новый API

Разбиение I8.8 — `D-147`.

- Перевести на новый API пакет `admin/`, CLI, `identity_hub.testing`, examples и все тесты.
  Тесты запускаются с `-W error::DeprecationWarning` для модулей `identity_hub`, кроме
  отдельного модуля тестов алиасов.
- **DoD:** в наборе тестов нет предупреждений об устаревании, кроме тестов алиасов; тесты
  `admin/` зелёные.

### I8.9 Руководство и примеры на новом API

- Переписать `docs/guide`: в `03-accounts` разделы идут «получить → создать → создать массово →
  импорт → изменить → состояние → удалить», под каждым — один метод; в `09-proxies` — `group`.
  Обновить README, `docs/cli.md`, `docs/integrations.md`, examples и CHANGELOG
  (`[Unreleased]`, раздел Deprecated).
- Финализировать `docs/api-migration.md`.
- Требования владельца к руководству (2026-09-30), каждый пункт — на новых неймспейсах:
  - **Точка входа:** `IdentityHub` → `hub.start()` → `hub.space(...)`; карта неймспейсов
    `space.*` с назначением каждого (таблица «сущность → неймспейс → ключевые методы»).
  - **Типовые операции по каждой сущности** с исполняемым примером: сервис (объявление,
    регистрация в хабе, изменение), аккаунт (получить, найти, создать, создать массово,
    изменить, удалить), прокси (`space.proxies`, группы, привязка, здоровье), пул и аренда
    (`space.pools`, виды аренды и когда какую брать), секреты (`space.secrets`), импорт
    (`space.imports`), связи, данные, вход, верификация.
  - **Изменение состояний:** какие методы меняют каждый регион (`activate`, `retire`,
    `set_lifecycle`, `restore_standing`, `override`, `unpin` и методы I8.6/I8.7), кто
    инициирует переход; бан, разбан, заморозка, синхронизация статусов с платформой.
  - **Лимиты на действия аккаунта:** `OperationRule` и квоты, pacing пула, cooldown,
    карантин и классификатор ошибок — как объявить и как это проявляется в API.
  - Сохранить диаграммы архитектуры и машин состояний из текущего `docs/guide`.
- **DoD:** примеры исполняются; ссылки проверены; в документации нет устаревших имён вне
  migration guide; каждый пункт требований владельца выше покрыт разделом с примером.

### I8.10 Удаление алиасов перед релизом

- Удалить legacy-алиасы и `pool=` у `ProxyPolicy`. Allowlist `SpaceHandle` сводится к
  неймспейсам и `using`/`ref`. CHANGELOG — раздел Removed.
- Выполняется после всех задач I1–I6; `I7.1` зависит от этой задачи.
- **Выполняется досрочно** задачей `F6.4` из `RELEASE-FIXES-PLAN.md` (`D-151`).
- **DoD:** `dir(SpaceHandle)` совпадает с allowlist; полный gate зелёный; migration guide
  отражает удаление.

---

## Граф зависимостей

```text
I0.1 → I0.2a → I0.2b
I0.2b → I0.3
I0.1 + I0.2b → I0.4
I0.3 + I0.4 → I1.1
I0.2b + I0.4 → {I1.2a → I1.2b, I1.3}
I0.3 + I1.3 → I1.4a → I1.4b

I0.4 → I2.1 → I2.2a → I2.2b
I0.2b + I2.1 → I2.3 → I2.4

I0.3 + I0.4 + I1.2b → I3.1a → I3.1b → I3.2 → I3.3

I0.2b + I0.4 + I1.2b + I2.3 → I4.1
I3.3 + I4.1 → I4.2
I2.4 + I4.1 → I4.3

I0.4 → I5.1
I4.3 → I5.2
I1.2b + I5.2 → I5.3

I1.3 + I2.2b + I2.4 → I6.1
I1.1 + I5.1 → I6.2
I1.4b + I3.2 → I6.3
I5.3 + I6.1 + I6.2 + I6.3 → I6.4
I1.4b + I2.4 + I3.3 + I4.3 + I5.3 → I6.5

I0.1 → I8.1 → I8.2 → {I8.3, I8.4}
I8.3 → I8.5
I8.1 → {I8.6, I8.7}
I8.3 + I8.4 + I8.5 + I8.6 + I8.7 → I8.8a → I8.8b → I8.9 → I8.10

I5.1 + I5.2 + I5.3 + I6.4 + I6.5 + I8.10 → I7.1 → I7.2
```

Порядок строк в `IMPROVEMENTS-PROGRESS.md` топологический. Следующая задача — первая `todo`,
у которой все зависимости имеют статус `done`. Независимые ветки можно брать в ином порядке,
если это зафиксировано в текущей задаче и исполнитель не смешивает их в одном коммите.
