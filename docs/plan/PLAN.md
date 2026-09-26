# Identity Hub — план имплементации v1

Источник требований: [`identity-hub-requirements.md`](../../identity-hub-requirements.md) (далее — ТЗ).
Схема БД: `identity_hub_models.py` → переносится в `src/identity_hub/postgres/models.py` (задача P0.2).
Прогресс: [`PROGRESS.md`](PROGRESS.md). Решения и отклонения: [`DECISIONS.md`](DECISIONS.md).
Протокол автономной работы: [`CLAUDE.md`](../../CLAUDE.md).

Этот файл — **неизменяемая спецификация задач**. Статусы здесь не ведутся — только в `PROGRESS.md`.
Менять план можно только записью в `DECISIONS.md` (новая задача, разбиение, отмена) с последующей правкой этого файла.

---

## 0. Архитектурные опоры (обязательны для всех задач)

1. **Раскладка** — `src/identity_hub/`, пакеты по ТЗ §5.2:
   ```
   src/identity_hub/
     __init__.py          публичный API (реэкспорт)
     _ids.py              new_id() = uuid7 (3.14) | uuid_utils | ошибка
     enums.py             все StrEnum (Lifecycle, Standing, AuthState, …, Severity)
     errors.py            иерархия исключений
     clock.py             Clock (для юнит-тестов ядра; в БД — всегда now())
     crypto/              envelope-шифрование, SecretsProvider, Keyring
     domain/              RegionMachine, VerificationFlow, admissibility, search_text, query parser
     service.py           Service, OperationRule, Quota, Pacing, ProxyPolicy, VerificationSpec, Tier
     protocols.py         Authenticator, Probes, ErrorClassifier, LoginContext, SecretsProvider, EventConsumer
     events/              каталог событий, EventDraft, маски, scrubber секретов
     store/               ABC Store (порт хранилища) + DTO
     hub.py               IdentityHub, SpaceHandle
     pool.py, lease.py, operation.py
     proxies.py, auth.py, verifications.py, sync.py, importing.py
     read/                search, identity_card, summaries, lease_stats
     dispatch/            диспетчер, webhooks, notifications (ядро)
     logging.py           LeaseContext, structlog-процессор, logging-фильтр, sink
     maintenance.py, doctor.py, metrics.py
     cli.py               identity-hub: migrate | dispatcher | maintenance | doctor | rotate-key
     postgres/            models.py, store.py (весь SQL), migrations/ (alembic), listen.py
     alembic.py           exclude_identity_hub
     telegram_notify/     канал aiogram (extra)
     testing/             MemoryStore, фабрики, conformance-набор (pytest-плагин)
   ```
2. **Весь SQL живёт в `identity_hub.postgres.store`** за внутренним ABC `Store` (ядро ничего не знает об asyncpg).
   Это требуется ТЗ §10.3: conformance-набор гоняется против PostgreSQL и против `MemoryStore`.
3. **Rich-ядро без БД**: машины регионов и верификаций — чистые функции/классы, возвращают «решение»
   (новое состояние + события + побочные эффекты), которое Store применяет атомарно.
4. **Транзакции** по ТЗ §7: собственные транзакции для фактов внешнего мира, savepoint в сессии пользователя
   для доменных решений; `set_config('ih.space', slug, true)` в каждой транзакции; никаких блокировок на время
   сетевого I/O; шифрование — до открытия транзакции.
5. **Время**: сравнения TTL/окон/сроков — только `now()` БД; `time.monotonic()` — только самоограждение аренды.
6. **Секреты**: `SecretStr`, никогда не попадают в события/логи/repr; `token` аренды — тоже.
7. **Каждая задача** = код + тесты + зелёные проверки (`ruff`, `mypy --strict`, `pytest`) + коммит.

### Маркеры тестов
- `unit` — без БД, быстрые.
- `pg` — PostgreSQL 16 через testcontainers (или `IH_TEST_DATABASE_URL`).
- `conformance` — общий набор, параметризован по Store (`pg`, позже `memory`).
- `load` — нагрузочные, запускаются явно.

### Команда проверки (Definition of Done для любой задачи)
```
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -m "not load"
```

---

## Фаза 0 — Каркас

### P0.1 Проект, инструменты, git
- `git init`, `.gitignore`, uv-проект `identity-hub`, `requires-python >=3.12` (dev-venv 3.14), src-раскладка.
- Зависимости ядра: `pydantic>=2`, `sqlalchemy[asyncio]>=2.0`, `cryptography`, `uuid-utils; python_version<'3.14'`.
- Extras: `postgres` (`asyncpg`), `alembic`, `telegram` (`aiogram>=3`), `structlog`, `webhooks` (`httpx`).
- Dev: `pytest`, `pytest-asyncio` (mode=auto), `testcontainers[postgres]`, `ruff`, `mypy`, `hypothesis`, `respx`.
- Настройки ruff (line 110, правила E,F,I,UP,B,SIM,ASYNC,RUF), mypy strict + плагин pydantic.
- **DoD**: `uv sync --all-extras`; команда проверки проходит на пустом тесте.

### P0.2 Перенос схемы и базовые типы
- `identity_hub_models.py` → `src/identity_hub/postgres/models.py`; исправить импорт `sqlalchemy.orm` (сейчас после класса `XID8`).
- Вынести StrEnum-ы в `identity_hub/enums.py`, модели их импортируют. `new_id()` → `_ids.py` (без тихого фолбэка на uuid4 — ошибка импорта, см. D-006).
- Поддержка `db_schema` (отдельная схема Postgres) через `schema_translate_map`.
- **DoD** (`pg`): `metadata.create_all` на PG16 проходит; тест: 24 таблицы, все CHECK/индексы из ТЗ §8.5 существуют.

### P0.3 Тестовая инфраструктура
- `tests/conftest.py`: session-фикстура PG16 (testcontainers, либо `IH_TEST_DATABASE_URL`), `async_engine`, чистка таблиц между тестами (TRUNCATE … CASCADE), фикстура `hub` / `space`.
- `docker-compose.yml` с postgres:16 для ручной разработки.
- Пример-сервис `tests/fixtures/services.py` (`FAKE_TG`, `FAKE_EXCHANGE`) с операциями, квотами, верификациями — используется всеми тестами.
- **DoD**: `pytest -m pg` поднимает контейнер и проходит smoke-тест.

### P0.4 Миграции Alembic и `exclude_identity_hub`
- Своя ветка миграций в `postgres/migrations`, своя таблица версий `ih_alembic_version`.
- Миграция 0001: `CREATE EXTENSION IF NOT EXISTS pg_trgm` + все таблицы.
- `identity_hub.alembic.exclude_identity_hub` (include_object для env.py пользователя).
- Программный `migrate(url | engine, schema=None)`.
- **DoD** (`pg`): upgrade на пустой БД; `alembic check`/autogenerate не видит разницы с metadata; тест exclude-хелпера.
- ТЗ: §8.1, §8.2.

---

## Фаза 1 — Ядро без БД (всё `unit`)

### P1.1 Ошибки и примитивы
- `errors.py`: `IdentityHubError` → `AlreadyExists`, `NotFound`, `SpaceMismatch`, `TransitionNotAllowed`, `ConcurrentModification`,
  `OperationNotAllowed(blockers)`, `QuotaExhausted(kind, window, retry_at)`, `LeaseUnavailable`, `LeaseLost`, `StaleFencing`,
  `ChallengeAlreadyOpen`, `VerificationRequirementsNotMet`, `AttemptsExhausted`, `ValidationFailed(errors)`.
- `Clock` protocol + `SystemClock`, `FakeClock`.
- **DoD**: юнит-тесты сообщений и pickling исключений.

### P1.2 RegionMachine (lifecycle, standing, auth, availability, proxy health, delivery)
- `RegionSpec`: состояния, таблица переходов команд + guard-ы, severity по целевому состоянию, `confirm_leaving`, нормализация.
- Вход: текущая `RegionState` (state, code, until, changed_at, version, pinned_until, pending_observation) + изменение
  (`Command` | `Observation(source, state, code, raw_status, observed_at, until)` | `Override(reason, actor, pin_for)`).
- Выход `RegionDecision`: new_state | no-op, причина no-op (`stale`, `pinned`, `unknown_status`, `awaiting_confirmation`),
  события (`*.changed`, `identity.state.overridden`, `identity.state.drift`), обновления служебных полей.
- Правила: наблюдение из любого состояния (FR-9.1); `observed_at < changed_at` → отброс (FR-9.2); pin → фиксация synced/raw_status + drift (FR-9.3);
  confirm_leaving — два наблюдения подряд (FR-9.4); неизвестный статус → предупреждение (FR-9.6); override требует reason+actor.
- Спецификации всех диаграмм ТЗ §3.1–3.4, 3.7, 3.8. `session_invalid` не трогает standing.
- **DoD**: таблично-параметризованные тесты по каждой стрелке диаграмм + недопустимые переходы; hypothesis-тест «наблюдения в любом порядке не нарушают инварианты».

### P1.3 VerificationFlow, VerificationSpec, VerificationTier
- Машина ТЗ §3.5; `requires`, `mode auto|manual`, `valid_for`, `max_attempts`, `reject_codes`, `evidence` (Pydantic-схема).
- `start` проверяет requires и попытки; `start` из `pending` — возобновление; попытка растёт на `submit` (FR-10.3).
- Нормализация `verified → missing ⇒ revoked` (FR-9.5); вычисление tiers (FR-10.2); неизвестные факторы игнорируются (FR-10.6).
- **DoD**: юнит-тесты всех переходов, лимита попыток, зависимостей, tiers.

### P1.4 Декларация `Service` и протоколы расширения
- `Service(name, title, attributes, profile, credentials, session, profile_fields{filterable, searchable}, operations,
  rules: OperationRule, standing_codes, verifications, tiers, normalizers, authenticator, probes, error_classifier,
  lease_mode exclusive|shared, proxy_policy, profile_encrypted, importers)`.
- `Quota(limit, per, origin/tz)`, `Pacing(work: Quota, pause: (min,max) мин)`, `OperationRule(standing, needs_auth, verified, tier, quotas)`,
  `ProxyPolicy(sticky, pool, max_identities, on_proxy_down wait|rebind)`.
- `protocols.py`: `Authenticator.login(ctx)`, `.refresh(creds, session)`, `LoginChallenge`, `LoginContext.linked(role)`,
  `Probes.auth/standing/verification(factor)`, `ErrorClassifier.classify(exc) -> Fault(blame, observation, retry_after)`,
  `SecretsProvider`, `EventConsumer`, `FileImporter`.
- Валидация декларации при создании (ссылки на несуществующие факторы/операции, квота вида `probe` по умолчанию).
- **DoD**: юнит-тесты валидации; `FAKE_TG`/`FAKE_EXCHANGE` из P0.3 переписаны на реальные классы. ТЗ FR-1.2, §5.3.

### P1.5 Вычисление допустимости операции
- `evaluate(identity_snapshot, rule, quota_status, now) -> list[Blocker]` по ТЗ §3.9 (lifecycle, standing, auth, availability с ленивым `until`, factors ⊇ rule ∪ tier, квота).
- Интерфейс «компиляции» правила в фильтр выборки (`RuleFilter` DTO), который Store превратит в SQL (P3.1).
- **DoD**: юнит-тесты на каждый blocker и комбинации.

### P1.6 Данные аккаунта, search_text, парсер поиска
- Валидация `labels` (плоские scalar, FR-3.3), `tags`, `attributes`/`profile` по схемам сервиса; разделение профиля на фильтруемую и шифруемую части (FR-3.2).
- `build_search_text(identity, service)` (FR-11.3): external_ref, короткий id, searchable-поля профиля, значения меток, заметка.
- `parse_query("service:instagram status:active country:UA farm:3 olena")` → `SearchQuery` (FR-11.1): известные ключи регионов, профиль/метки, свободный текст (слова < 3 символов отбрасываются), полный UUID → точный поиск.
- **DoD**: юнит-тесты, включая hypothesis на парсер.

### P1.7 Криптография
- AES-256-GCM envelope: DEK на запись, wrap мастер-ключом, `key_id`, AAD = `(table, identity_id|owner_id, slot|factor)` (S-1..S-3).
- `SecretsProvider`: `EnvSecretsProvider`, `FileSecretsProvider`, `StaticSecretsProvider` (тесты); `Keyring` с активным и старыми KEK.
- `seal(model: BaseModel, aad) -> EncryptedPayload`, `open_(payload, schema, aad)`; корректная сериализация `SecretStr`.
- **DoD**: тесты: roundtrip, подмена AAD/ciphertext → ошибка, расшифровка старым KEK после ротации, `repr` без секретов.

### P1.8 Каталог событий
- `EventType` каталог ТЗ FR-13.3 с флагом `external`, severity по умолчанию; `EventDraft`; сопоставление масок (`pool.capacity.*`).
- Scrubber: запрет `SecretStr`/bytes/полей `token|password|secret` в `data` (S-8) — ошибка в тестах, вырезание в проде.
- **DoD**: юнит-тесты каталога и scrubber-а.

---

## Фаза 2 — Хранилище и базовые записи

### P2.1 IdentityHub, SpaceHandle, транзакции
- `IdentityHub(sessionmaker, secrets, services, admin_base_url, retention, db_schema, metrics, clock)`; `await hub.start()` — синхронизация `ih_services` (FR-1.3).
- `hub.space(slug, title=None)` (создание/получение, свой UUID допускается), `space.using(session)`.
- Хелпер транзакций: своя транзакция | `begin_nested()` в сессии пользователя (T-1); `set_config('ih.space', …)` (T-3); смена пространства в связанной сессии → `SpaceMismatch`.
- **DoD** (`pg`): тесты владения сессией (откат пользователя откатывает register, но не собственные транзакции), T-3.

### P2.2 Outbox событий
- `Store.emit(tx, drafts)`: инкремент `ih_identities.event_seq` → `sequence`, вставка в `ih_events` (tx_id по умолчанию), `pg_notify('ih_events', …)` в той же транзакции.
- **DoD** (`pg`, `conformance`): событие видно тогда и только тогда, когда закоммичено изменение (G-5.1, G-4.1); sequence монотонен.

### P2.3 Регионы в БД
- Загрузка/создание `ih_region_states`, применение `RegionDecision` с оптимистической блокировкой по `version`
  (ретрай при `ConcurrentModification` для наблюдений), запись денормализованных колонок `ih_identities` и событий одной транзакцией (G-6.1).
- API: `space.set_lifecycle/retire` (команды), `space.observe(...)` (внутренний), `space.using(s).override(id, Override(...))`.
- **DoD** (`conformance`): устаревшее наблюдение, override с pin + drift, confirm_leaving, конкурентное наблюдение. ТЗ FR-9.1–9.4, 9.6, 9.8.

### P2.4 `register`
- Аккаунт + credentials + сессия (зашифрованы) + метки/теги/профиль/атрибуты + связи + начальные регионы + `identity.registered` — одна транзакция (G-4.3).
- Идемпотентность по `id` и `(service, external_ref)` глобально → результат `AlreadyExists`/`created=False` без изменений (FR-2.1–2.3).
- `profile_encrypted` (FR-3.2): нефильтруемые поля в зашифрованном виде.
- **DoD** (`conformance`): повторный register, конфликт external_ref между пространствами, внутри транзакции пользователя.

### P2.5 Изменение данных аккаунта
- `set_labels`, `set_tags`, `update_profile`, `set_attributes`, `note`, `delete` — с пересчётом `search_text`, событием `identity.updated`/`identity.deleted`.
- `identity_data`: неймспейсы, Pydantic-схема и версия схемы, оптимистическая блокировка (FR-3.6).
- **DoD** (`pg`): валидация, конфликты версий `identity_data`, удаление запрещено при входящих связях.

### P2.6 Связи
- `link/unlink` (роль, одна связь на `(аккаунт, роль)`, то же пространство, без самосвязи), события `identity.linked/unlinked`.
- Read-модели `links_of`, `link_summary(ids)` (один запрос), `impact_of(id, max_depth)` (рекурсивный CTE).
- **DoD** (`pg`): все инварианты FR-4.1, 4.2, 4.4.

---

## Фаза 3 — Аренда

### P3.1 Pool и SQL выборки кандидатов
- `space.pool(name, service, tags, labels, profile, pacing, proxy_policy, ttl=60s, heartbeat=20s, safety_margin=20s)`; валидация `heartbeat ≤ TTL/3`, `safety_margin ≥ heartbeat` (G-1.2).
- Компиляция фильтров: теги (`@>`), метки/профиль (`@>` по GIN), `for_operation` (RuleFilter из P1.5), `among` (список id | `Select`), доступность прокси при `on_proxy_down=wait`, ленивое `availability_until < now()`.
- Выборка `FOR UPDATE SKIP LOCKED LIMIT 1` использует индекс `ix_ih_identities_pick`; порядок выдачи — см. D-007.
- **DoD** (`pg`): EXPLAIN-тест использует частичный индекс; фильтры покрыты тестами.

### P3.2 Захват аренды
- В одной короткой транзакции: кандидат → `fencing_seq + 1` → upsert `ih_leases` (перезапись истёкшей строки с закрытием её истории как `expired` временем `expires_at`, FR-6.8) → строка `ih_lease_history` (G-4.2) → событие `lease.acquired` (external=false).
- `idempotency_key` (FR-6.5), `shared`-режим (FR-6.6), `wait` (таймаут, backoff + пробуждение по NOTIFY освобождения — опционально).
- `lease_id` = UUIDv7, `token` = uuid4 (не логируется, FR-6.4).
- **DoD** (`conformance`): гонка двух acquire (G-1.1), повтор по idempotency_key, перезахват истёкшей.

### P3.3 Жизненный цикл `Lease`
- `async with pool.lease(...) as lease`: heartbeat-задача на отдельной сессии (T-2), продление `expires_at` + сброс счётчиков операций в историю (FR-6.7).
- Самоограждение по `time.monotonic()`: дедлайн `last_ok + TTL − safety_margin` → `on_lost()` + `LeaseLost` в блоке (отмена задачи-владельца, преобразование `CancelledError` → `LeaseLost`) (FR-6.3).
- Выход: `released` / `failed` (с `last_error`) / `lost`; release по token идемпотентен, чужой token — no-op.
- `LeaseContext` в contextvars (FR-12.1 — база для P7.1).
- **DoD** (`pg`): heartbeat продлевает; искусственная задержка heartbeat → LeaseLost раньше TTL; история закрыта с правильной причиной.

### P3.4 Fencing-записи и секреты под арендой
- `lease.credentials()`, `lease.session(slot)` — расшифровка только здесь (FR-5.2); `lease.save_session(...)` с проверкой fencing (FR-5.3, G-1.4) + `session.saved`; `lease.mark_authenticated()`.
- `space.revoke_lease(identity_id, reason, actor)` (FR-6.9) — зомби-воркер получает `LeaseLost`/`StaleFencing`.
- **DoD** (`conformance`): зомби после истечения не может сохранить сессию; revoke.

---

## Фаза 4 — Операции, квоты, pacing, здоровье

### P4.1 Квоты
- `date_bin` окна с настраиваемым origin (таймзона платформы), несколько правил на вид; атомарное списание всех правил в savepoint
  (`INSERT … ON CONFLICT DO UPDATE SET used = used + 1 WHERE used < limit RETURNING`) (FR-7.2, G-2.1, G-2.2).
- Pacing — тот же механизм с `kind='__pacing__'`; `identity.quota.exhausted` один раз за окно.
- Ключи операций `ih_operation_keys` (FR-7.5).
- **DoD** (`conformance`): гонка за последнюю единицу квоты; повтор ключа не списывает; окно «в час + в сутки».

### P4.2 `lease.operation(kind)`
- Проверка допустимости (свежий снимок) → списание квот и pacing **в своей транзакции до вызова платформы** (FR-7.1).
- Успех → снижение `error_score`; исключение → `ErrorClassifier` → вина `identity|proxy|request|session_invalid|identity_banned` → наблюдение соответствующего региона/прокси (FR-7.3).
- `op.cool_down(retry_after)` — только удлинение (FR-7.4); pacing → `paused` на случайную паузу; `error_score ≥ порога` → `quarantined`.
- Счётчики операций в памяти аренды; события `operation.*` (журнал).
- **DoD** (`pg`): все ветки вины; квота не откатывается при исключении пользователя; FR-7.6 (нет API сброса).

---

## Фаза 5 — Прокси, вход, верификации, сверка, импорт

### P5.1 Прокси и их здоровье
- `space.proxies.add/add_many(pool, url|parts)`: дедупликация по fingerprint, логин/пароль зашифрованы (FR-8.1).
- Здоровье: счёт ошибок, карантин с backoff × 2 перепроверки (`next_check_at`), `dead` после предела, выход из `dead` с подтверждением (FR-8.4, 3.7) через RegionMachine из P1.2; `proxy.status.changed`.
- Ручная перепроверка/override; проба прокси — пользовательский колбэк.
- **DoD** (`pg`): цепочка alive → quarantined → dead → alive.

### P5.2 Привязка прокси к аккаунтам
- `sticky`: при первой аренде наименее загруженный прокси с `bound < max_identities` (FR-8.2) — в транзакции захвата аренды.
- `on_proxy_down=wait|rebind` (FR-8.3); события `proxy.binding.changed`, `proxy.pool.exhausted`; `lease.proxy` (расшифрованный URL).
- **DoD** (`conformance`): конкурентные привязки не превышают `max_identities`; rebind.

### P5.3 Оркестрация входа
- `space.run_logins(limit)`: аккаунты `no_session|session_invalid` → аренда → `Authenticator.login(LoginContext)`; связанные аккаунты арендуются в порядке «зависимый → связанный» (FR-4.3).
- `LoginChallenge` → `auth=challenge`, запись `ih_challenges` с зашифрованным контекстом, один открытый challenge (FR-5.5); `answer_challenge(id, answer)`; истечение challenge.
- Ручной путь: `save_session` + `mark_authenticated` (FR-5.6). События `identity.login.*`, `identity.challenge.*`.
- **DoD** (`pg`): фейковый аутентификатор — успех, ошибка, challenge → ответ, повторный challenge запрещён.

### P5.4 Верификации в БД
- `lease.verification(name)`: `start(evidence)` (evidence зашифрован до вызова платформы, FR-10.4), `submit`, `approve/reject(code)`, `reconcile(observed)`.
- `verified_factors` в той же транзакции (FR-10.5); `identity.verification.changed`; требования платформы `identity.requirement.added/resolved`.
- **DoD** (`pg`): полный цикл KYC, expiry (через maintenance-функцию), лимит попыток.

### P5.5 Пробы и сверка
- `space.resync(id, regions)` — внеочередная проба под арендой; `space.reconcile(regions, not_synced_for, limit)` — фон, `wait=False`, квота вида `probe` (FR-9.7).
- Сохранение источника/автора/причины/`raw_status`/`synced_at` (FR-9.8).
- **DoD** (`pg`): pinned override + проба → drift, без изменения состояния.

### P5.6 Секреты для админки
- `space.update_credentials(...)` → `identity.credentials.updated`; `space.reveal_secret(id, field, actor)` → `identity.secret.revealed` (S-7, FR-5.2).
- **DoD** (`pg`): листинги/карточки без секретов (проверка сериализации всех read-моделей).

### P5.7 `import_many`
- Поток записей (`AsyncIterable[ImportRecord]`), валидация схемами до БД, пачки по 500 в отдельных транзакциях (G-4.4), отчёт `created/skipped/failed` с причинами (FR-2.4).
- `on_existing=skip|update_session|fail`, связи по `external_ref` (включая внутри пачки), прокси с дедупликацией, начальные верификации, начальные состояния (FR-2.5), `identity.imported` на пачку, хук `on_batch_committed(session, created_ids)` (FR-2.7).
- `FileImporter` протокол (FR-2.6) + пример JSON-cookies импортёра в тестах. Шифрование пачки — до транзакции (T-5).
- **DoD** (`conformance`): повтор импорта → всё в skipped; смешанная пачка; 10 000 записей укладываются в цель (проверка в P10.3).

---

## Фаза 6 — Поиск и read-модели

### P6.1 Поиск
- `space.search(query | SearchQuery, limit, after)`: точные фильтры `@>` (FR-11.2), trigram по `search_text` (FR-11.3), регионы, keyset `(created_at, id)`, ограниченный счётчик до 1000/«1000+» (FR-11.4), UUID → точный поиск.
- **DoD** (`pg`): тесты фильтров и пагинации; EXPLAIN использует GIN-индексы.

### P6.2 Карточки и статистика
- `identity_card(id)` (FR-11.5), `pool_summary` (FR-11.6), `lease_history(id)`, `lease_stats(id, since)` (FR-11.7), `reindex_search(service)` (FR-11.8).
- **DoD** (`pg`): состав карточки, отсутствие секретов и токенов.

---

## Фаза 7 — Логи

### P7.1 Контекст логов
- `identity_hub.logging`: `LeaseContext` из contextvars, `structlog`-процессор, `logging.Filter` (FR-12.1).
- **DoD** (`unit`): записи внутри аренды получают `lease_id, identity_id, service, pool, holder`.

### P7.2 Sink `ih_lease_logs`
- Минимальный уровень, лимит строк на аренду, буфер → запись пачкой на heartbeat и release, ошибки sink-а подавляются (FR-12.2).
- **DoD** (`pg`): лимит, сброс на heartbeat, падение БД sink-а не рвёт аренду.

---

## Фаза 8 — Доставка событий

### P8.1 Диспетчер
- Курсоры `ih_event_cursors` (строка блокируется `FOR UPDATE SKIP LOCKED` на пачку, G-5.5), чтение `(tx_id, id) > курсор AND tx_id < pg_snapshot_xmin(pg_current_snapshot())` (G-5.2).
- Пробуждение `LISTEN ih_events` через прямое asyncpg-соединение, фолбэк — опрос 2 с (FR-13.6); встраиваемый режим и `identity-hub dispatcher` (FR-13.5).
- `hub.events.subscribe(name, fn, types)` — Python-колбэки, at-least-once.
- **DoD** (`pg`, `conformance`): тест «транзакция начата раньше, закоммичена позже — событие не пропущено»; два диспетчера не дублируют пачку.

### P8.2 Агрегированные события
- `pool.capacity.low/empty` (пороги на пул), `worker.leases_lost` (агрегат за окно), severity по спецификации региона (FR-13.4).
- **DoD** (`pg`).

### P8.3 Исходящие вебхуки
- Endpoints: URL (только https, кроме allowlist хостов для dev), маски типов, фильтр (сервис, метки), min severity (FR-14.1).
- Подпись `X-Identity-Hub-Signature: t=…,v1=…`, секрет зашифрован, ротация с двумя активными секретами (FR-14.2, S-9) + `verify_signature` хелпер для получателей.
- Доставка: одна строка на `(endpoint_id, event_id)`, httpx, таймаут 10 с, без редиректов, ретраи exp+jitter до 24 ч, отключение после N неудач подряд + `webhook.endpoint.disabled`, журнал попыток, ручной повтор (FR-14.3–14.5).
- **DoD** (`pg` + `respx`): 2xx, 5xx → ретрай, отключение, ручной повтор, проверка подписи.

### P8.4 Telegram-уведомления
- Каналы (`bot_token` зашифрован, `chat_id`, `thread_id`), правила (типы, severity, фильтр `service`/`data.*`/метки, дайджест|немедленно, шаблон) — CRUD (FR-15.1–15.3).
- Дайджест: одно открытое уведомление на правило до конца окна (FR-15.4); шаблоны по умолчанию + ссылка на карточку (FR-15.5).
- Отправка через aiogram с учётом `retry_after` (429), ретраи по политике вебхуков (FR-15.6); без секретов (FR-15.7).
- Фейковый транспорт бота для тестов.
- **DoD** (`pg`): немедленное, дайджест, 429, фильтр по `data.to`.

---

## Фаза 9 — Эксплуатация

### P9.1 Maintenance
- Задачи ТЗ §8.9 с периодичностью; сроки хранения из настроек hub (D-004); встраивание в диспетчер и `identity-hub maintenance`.
- **DoD** (`pg`): каждая задача покрыта тестом (истёкшие `until`, незакрытые аренды → `expired` по `expires_at`, очистки).

### P9.2 CLI
- `identity-hub migrate --url`, `dispatcher`, `maintenance [--once]`, `doctor [--fix]` (G-6.2: регионы vs денормализация, `verified_factors`, `event_seq`), `rotate-key` (постепенная перешифровка, S-3).
- **DoD** (`pg`): doctor находит и чинит искусственное расхождение; rotate-key перешифровывает все зашифрованные таблицы.

### P9.3 Метрики
- `MetricsHook` protocol (без зависимости от Prometheus): длительность lease/operation, отказы по причинам, доступные аккаунты по пулу, отставание курсоров, очередь доставок (§10.2).
- **DoD** (`unit`/`pg`): хук вызывается в нужных точках.

---

## Фаза 10 — Тестовый пакет, качество, релиз

### P10.1 `MemoryStore`
- In-memory реализация `Store` (без SQL): регионы, аренда, квоты, операции, события, register/import. Поиск — упрощённая подстрока.
- **DoD**: весь `conformance`-набор зелёный против `memory` и `pg`.

### P10.2 `identity_hub.testing`
- Фабрики (`make_identity`, `make_service`), pytest-плагин с фикстурами и экспортом conformance-набора для пользователей.
- **DoD**: пример пользовательского теста в `examples/` использует плагин.

### P10.3 Нагрузочные тесты
- Сценарии ТЗ §10.1 (`-m load`): lease p95 ≤ 20 мс (10k аккаунтов, 50 воркеров), operation p95 ≤ 10 мс, поиск p95 ≤ 100 мс (100k), импорт 10k ≤ 60 с, задержка события p95 ≤ 1 с.
- Результаты → `docs/perf.md`. Не достигнуто — задача-исправление в DECISIONS.

### P10.4 Документация и примеры
- README (установка через uv, быстрый старт), `examples/telegram_service.py` — пример из ТЗ §11 как исполняемый интеграционный тест, справка по CLI, интеграция с Alembic и Dishka, CHANGELOG.

### P10.5 Финальный аудит
- `docs/traceability.md`: каждый FR/G/T/S из ТЗ → тест(ы). Непокрытые — новые задачи.
- Проверка публичного API на соответствие §11, `__all__`, версии 1.0.0rc1.
- **DoD**: матрица без пробелов, все проверки зелёные.

---

## Граф зависимостей

```
P0.1 → P0.2 → P0.3 → P0.4
P0.2 → P1.1 → {P1.2, P1.3, P1.4, P1.6, P1.7, P1.8}
P1.4 → P1.5
P0.4 + P1.2 + P1.4 + P1.7 + P1.8 → P2.1 → P2.2 → P2.3 → P2.4 → {P2.5, P2.6}
P1.5 + P2.4 → P3.1 → P3.2 → P3.3 → P3.4
P3.3 → P4.1 → P4.2
P4.2 → P5.1 → P5.2
P3.4 + P2.6 → P5.3
P1.3 + P3.4 → P5.4
P5.3 + P5.4 + P4.2 → P5.5
P3.4 → P5.6
P5.2 + P5.4 + P2.6 → P5.7
P2.5 + P3.3 → P6.1 → P6.2
P3.3 → P7.1 → P7.2
P2.2 → P8.1 → {P8.2, P8.3 → P8.4}
P5.4 + P3.3 → P9.1;  P9.1 + P8.1 → P9.2;  P4.2 → P9.3
все P2–P9 → P10.1 → P10.2 → P10.3 → P10.4 → P10.5
```
Порядок в `PROGRESS.md` — топологический; следующая задача = первая `todo`, у которой все зависимости `done`.
