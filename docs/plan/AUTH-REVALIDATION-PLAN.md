# Identity Hub — план очереди перепроверки аккаунта (`AR`)

Источник требований: [`../isue/auth-revalidation-review.md`](../isue/auth-revalidation-review.md) —
ревью RFC с экспериментами E1–E6, ресерчем и рекомендуемым решением (слои L0–L3).
Исходное RFC: [`../isue/auth-revalidation-architecture.md`](../isue/auth-revalidation-architecture.md).
Воспроизведение экспериментов: [`../isue/auth-revalidation-experiments.py.txt`](../isue/auth-revalidation-experiments.py.txt).
Прогресс: [`AUTH-REVALIDATION-PROGRESS.md`](AUTH-REVALIDATION-PROGRESS.md).
Решения: [`DECISIONS.md`](DECISIONS.md) (`D-335`).
Протокол автономной работы: [`CLAUDE.md`](../../CLAUDE.md).

Этот файл — спецификация задач `AR<фаза>.<номер>`. Статусы ведутся только в
`AUTH-REVALIDATION-PROGRESS.md`. Изменение объёма, разбиение или отмена задачи оформляются записью
`D-NNN`, затем синхронно отражаются в плане, матрице трассировки (раздел 7) и прогрессе.

## Откуда план

Потребитель `checker_account` обходит терминальные статусы Identity Hub (`restore_standing`,
`activate`, `verify_credential`, `enable_credential`) перед явной перепроверкой аккаунта. Инцидент:
`auth: no_session → session_invalid` отвергнут графом `Command` уже после вызова провайдера, откат
транзакции оставил операцию `running`, повтор получил `LeaseUnavailable(BUSY)`, прогон длился
~3 минуты. Ревью показало, что класс дефекта шире хотфикса: вердикт провайдера применяется как
`Command` в `verify_session` (`auth.py`), `adopt_session` и `_set_auth`, а из `challenge` тот же
сбой остаётся. Решение (`D-335`): вердикт — это `Observation`; допуск диагностики отделён от допуска
нагрузки; граф команд не расширяется.

Слои из ревью: **L0** → `AR0.1`; **L1** → `AR1.1`, `AR1.2`; **L2** → `AR2.1`, `AR2.2`;
**L3** → `AR3.1`–`AR3.3`; закрытие → `AR4.1`.

## Границы (что в очередь НЕ входит)

- **L4**: политика выхода из `banned/deleted` по наблюдению (`standing_exit`,
  `login_success_implies_standing_good`). Разбан остаётся ручным. Порядок проверок `sticky` и
  `confirm_leaving` не меняется; мёртвый `BANNED ∈ confirm_leaving` остаётся как есть и
  документируется в `AR4.1` (решение владельца нужно отдельной записью `D-NNN`).
- **«RFC B»** из ревью: шифрованные артефакты, `projection_status`, projection worker. Вместо них —
  дешёвая сетка `AR1.1`.
- **Родительская таблица попыток.** Идемпотентность — через `request_key` у операций (`AR3.3`).
- **Изменение поведения `login`**: технический отказ `login` по-прежнему переводит `auth` в
  `no_session` (`auth.py`, ветка `LoginFailure`), даже если вход начинался из `session_invalid`.
  `auth.revalidate()` этого не наследует, потому что не использует `logging_in`; сам `login` не трогаем.
- **Репозиторий `checker_account`.** Код потребителя не меняется; в `AR4.1` — только памятка, какие
  обходы можно убрать.
- Версия пакета и публикация: очередь не выпускает релиз.

---

## 0. Правила реализации

Действуют правила раздела 0 `SERVER-ADMINS-PLAN.md` (падающий тест первым, оба бэкенда, документация
и CHANGELOG в том же коммите). Дополнительно:

1. **Тест первым.** Эксперименты ревью — готовые сценарии. Файл
   `docs/isue/auth-revalidation-experiments.py.txt` не копировать как есть (он печатает матрицы):
   переписать в настоящие тесты с `assert` в `tests/conformance/` (`hub_under_test`, `space`).
2. **Оба бэкенда.** Поведение проверяется на `MemoryStore` и PostgreSQL (`IH_TEST_BACKENDS` по
   умолчанию — оба). Для `MemoryStore` операции читаются из `hub._store._state.provider_operations`,
   для PostgreSQL — SQL к `ih_provider_operations` через `hub._store.sessionmaker()`.
3. **Безопасность данных.** В событиях, `result` операций, ошибках и логах — только машинные коды и
   вердикты. Payload сессии и credentials, текст исключений провайдера — никогда. На каждый новый путь —
   тест, что секрет не попал в событие и в `result`.
4. **Граф команд не расширяется.** Внешний факт никогда не добавляет переход в `transitions`.
   Если для факта кажется нужным новый переход — остановиться и записать `D-NNN`.
5. **Схема БД — только новой миграцией** (`D-012`). Единственная ожидаемая миграция очереди —
   в `AR3.3` (`request_key`); номер — следующий после `SCHEMA_HEAD` (на момент плана `0039`), если за это
   время появились новые, взять следующий свободный. Обновить `SCHEMA_HEAD`, alembic-тесты,
   ревизию схемы в `CHANGELOG`.
6. **Публичный API.** Новые публичные имена — в `identity_hub/__init__.py`, `tests/test_public_api.py`,
   `tests/test_space_surface.py`, `tests/typing/`, `docs/guide/api.md`. Новые типы событий — в
   `docs/guide/10-events.md` и контракте событий (`tests/event_data_contract.py`).
7. **Хотфикс владельца.** В рабочем дереве на старте есть незакоммиченные правки
   `src/identity_hub/domain/region_specs.py` (переход `NO_SESSION → SESSION_INVALID`) и
   `tests/conformance/test_session_first_auth.py`
   (`test_adopt_invalid_uploaded_default_from_no_session_is_terminal`). Тест остаётся и должен
   проходить после `AR0.1`; **переход в графе удаляется** (см. `AR0.1`).
8. **Коммит закрытия:** `fix(AR0.1): …` для `AR0.1`, `feat(<ID>): …` для остальных; в теле — ссылки на
   эксперименты ревью (`E1`…`E6`) и `D-335`.
9. **Проверка:** `bash scripts/check.sh > .check.log 2>&1; rc=$?; tail -3 .check.log; [ $rc -eq 0 ]`.
   Тесты со временем и конкурентностью — полным набором 2–3 раза перед закрытием задачи.

---

## Фаза AR0 — вердикт провайдера как наблюдение (L0, P0)

### AR0.1 Вердикт провайдера как `Observation`, защита `challenge`

Ревью, эксперименты E1, E3, E6.

**Проблема.** `auth.verify_session` и `auth.adopt_session` применяют вердикт `session_invalid` как
`Command` (`auth.py`, вызовы `apply_region_change(..., Command(AuthState.SESSION_INVALID, ...))`).
Граф `AUTH` отвергает переходы `no_session → session_invalid` и `challenge → session_invalid`;
терминализация операции идёт в той же транзакции, поэтому откатывается, операция остаётся `running`.
Одновременно обычная проба (`Observation`) способна затереть `challenge`.

**Первыми — тесты (красные), `tests/conformance/test_auth_verdict_observation.py`:**

- Матрица: исходное `auth ∈ {no_session, authenticated, session_invalid, challenge, logging_in}` ×
  метод `{verify_session, adopt_session}` (default-слот, зонд возвращает `SESSION_INVALID`). Ожидание:
  исключения нет, операция `probe:auth` в `succeeded`, `auth` после: для `no_session`, `authenticated`,
  `session_invalid` — `session_invalid`; для `challenge` и `logging_in` — без изменений; результат
  метода — `INVALID`. Состояние ставить явным `identities.override(Override("auth", "", state, ...))`
  (`create(session=...)` сразу даёт `authenticated`, это ловушка эксперимента E1).
- `sync.refresh(identity_id, regions=("auth",))` поверх `challenge` и `logging_in` с вердиктом
  `session_invalid` → исход региона `sticky`, `auth` не изменён, записано `identity.state.drift`.
- Unit автомата: `AUTH` + `Observation(session_invalid)` в `challenge`/`logging_in` → `Outcome.STICKY`;
  `Observation(authenticated)` из `challenge` → `APPLIED` (`sticky_exits`); `Command` из `challenge` и
  `logging_in` по-прежнему идёт по таблице переходов (sticky на команды не действует).
- Закреплённый (`pin`) регион `auth`: вердикт не применяется, drift записан, операция `succeeded`.
- Тест хотфикса владельца проходит без перехода в графе.

**Реализация:**

- `verify_session` и `adopt_session`: `Command(SESSION_INVALID, ...)` →
  `Observation(AuthState.SESSION_INVALID, observed_at=await tx.now(), source=ChangeSource.PROBE,
  code="session_invalid", actor=actor)`. Импорт `Observation`, `ChangeSource` в `auth.py`.
- `domain/region_specs.py::AUTH`: `sticky=_fs(CHALLENGE, LOGGING_IN)`,
  `sticky_exits=_fs(AUTHENTICATED)`; из `transitions[NO_SESSION]` убрать `SESSION_INVALID` (хотфикс).
- `_set_auth` (login) остаётся на `Command`: login владеет `logging_in` под своим lease. Остальные
  `Command` на `auth` (таймеры `NO_SESSION`, `mark_authenticated`) не трогать.
- `retryable`: `Command` нёс `retryable=False`, `Observation` — нет. Найти потребителей
  (`RegionInfo.retryable`, карточка, админка, `S18.1`); если значение нужно — сохранить его
  (расширить `Observation`/`RegionState` по существующему образцу) или записать `D-NNN` о том, что для
  вердикта-наблюдения `retryable=None`.
- `grep -rn "apply_region_change" src` и `grep -rn "Observation(" src`: проверить остальные пути,
  применяющие внешний факт как `Command`; найденное — исправить или занести в «Заметки» с причиной.
- Проверить, что код, полагавшийся на «проба выводит из challenge», не сломан (поиск по тестам и
  `docs/`); таймер истечения `challenge` идёт через `Command` и не затронут.

**Документация:** `docs/guide/04-states.md` — наблюдение не затирает `challenge` и `logging_in`
(исход `sticky`); `docs/guide/10-events.md` — если изменился состав `identity.state.drift`;
`CHANGELOG.md`.

- **DoD:** матрица зелёная на `MemoryStore` и PostgreSQL; ни один из путей `verify_session`/
  `adopt_session` не оставляет операцию `running`; `challenge` не затирается пробой; граф `AUTH` не
  содержит перехода под внешний факт; полный `scripts/check.sh` зелёный.

---

## Фаза AR1 — операция не теряется из-за проекции (L1)

### AR1.1 Сетка `projection_failed`: операция не остаётся `running`

Зависит от `AR0.1`. Ревью, раздел 6 (L1), риск D-220.

**Идея.** После `AR0.1` граф не откатывает вердикт-наблюдение, но остаются пути, где проекция может
упасть: `login` (`_set_auth`, `Command`), параллельное изменение (override оператора во время
операции), инфраструктурные ошибки. Если исключение проекции откатывает транзакцию вместе с
терминализацией операции, операция зависает в `running`. Нужна защита по образцу Brandur «atomic
phases» / Temporal «результат до применения», но без новой таблицы.

**Предварительно изучить и записать в «Заметки»:** модель ревизий операции (`D-220`,
`ProviderOperationRow.revision`, `_apply_operation`, `_finish_probe`, `_apply_login`) — допускает ли
она повторную запись в операцию той же ветвью claim (тот же `claim_id`/`claim_token`) после неудачной
транзакции проекции. Если допускает — реализовывать по плану ниже; если нет — записать `D-NNN`
с выбранным вариантом (например, терминализация операции в первой транзакции, проекция во второй).

**Первыми — тесты (красные):**

- Инъекция исключения в проекцию (подмена `apply_region_change` на функцию, бросающую
  `TransitionNotAllowed`, либо конкурентный `override`, делающий `Command` недопустимым) для путей
  `verify_session`, `adopt_session`, `_apply_probe` (`sync.refresh`), `login` (`_set_auth`): после
  исключения операция в `failed` с `error_code="projection_failed"`, `result` содержит безопасный
  вердикт (`{"verdict": ..., "projection": "failed"}`, без секретов), исключение пробрасывается
  прежнего типа, повторный вызов НЕ получает `LeaseUnavailable(BUSY)`.
- Секрет из payload сессии/исключения не попадает в `result` и событие.

**Реализация:** общий helper рядом с `_apply_operation` (`sync.py`): выполняет проекцию в транзакции;
при исключении открывает отдельную короткую транзакцию, терминализует операцию по текущему claim
(`failed/projection_failed`, безопасный `result`), затем пробрасывает исключение. Не глотать
исключение и не менять результат пути для вызывающего. Применить во всех четырёх путях.

- **DoD:** при любой исключительной проекции операция терминальна, повтор не блокируется, вердикт
  провайдера сохранён в `result`; оба бэкенда; `check.sh` зелёный.

### AR1.2 Recovery: `outcome_unknown` и метрика

Зависит от `AR1.1`. Ревью, E5.

**Идея.** Падение между ответом провайдера и коммитом оставляет `running`; сейчас recovery worker
переводит такую операцию в `failed/claim_expired` и вердикт теряется. Контракт («абсолютной durable
terminal-гарантии нет») — `outcome_unknown`, а не «ничего не произошло».

**Первыми — тесты:** E5 как регрессия на обоих бэкендах (для PostgreSQL истекать claim SQL:
`UPDATE ih_provider_operations SET claim_expires_at = now() - interval '1 second' WHERE id = :id`; helper
`_expire_provider_claim` уже есть в `tests/conformance/test_session_first_auth.py`): после
`workers.provider_operations.run_once()` операция терминальна и помечена как неизвестный исход
(`result={"outcome": "unknown"}`, `error_code="claim_expired"`), повтор того же логического запроса
не блокируется; воркер публикует счётчик `recovered`.

**Реализация:** найти политику recovery (`recover_provider_operations(policy=...)`, воркер
`provider_operations`); добавить `outcome: unknown` в безопасный `result` восстановленных сетевых
операций проб и входа (не меняя `error_code`); убедиться, что метрика/`WorkerRun.counts["recovered"]`
отражает число восстановленных. Схему не менять; если без колонки нельзя — `D-NNN`.

- **DoD:** восстановленная операция явно помечена как неизвестный исход; повтор разрешён; метрика
  `recovered` проверена тестом; оба бэкенда.

---

## Фаза AR2 — диагностический допуск (L2)

### AR2.1 `DiagnosticPolicy` и типизированные `Blocker`

Зависит от `AR0.1`. Ревью, раздел 6 (L2); `D-335`.

Модель без изменения поведения.

- `DiagnosticPolicy` (frozen dataclass; публичный): `lifecycles: frozenset[Lifecycle]`,
  `availability: frozenset[Availability]`. Значения по умолчанию:
  `lifecycles={provisioning, ready, retired}`, `availability={available, paused, cooling}`.
  `quarantined` в `availability` допустим **только если сервис перечислил его явно**; то же для
  редких lifecycle. Валидация: неизвестные значения → `ValidationFailed`; пустые множества → ошибка.
- `Service(..., diagnostics: DiagnosticPolicy | None = None)`: `None` → политика по умолчанию.
  Поле участвует в `_validate()` сервиса и в каталоге/описании сервиса, как `url` (`S11.1`) и
  `probe_timeout`.
- `Blocker` (frozen dataclass) + `BlockerKind` (StrEnum): `lifecycle`, `availability`, `standing`,
  `credential_missing`, `credential_expired`, `credential_disabled`, `credential_revoked`, `proxy`,
  `busy`, `not_found`. Поля: `kind`, `detail: str | None` (только безопасный машинный текст).
- **Первыми — тесты:** валидация и значения по умолчанию; `quarantined` отсутствует по умолчанию и
  появляется только по явной настройке; сериализация/сравнение; typing-тест; публичный экспорт.
- **DoD:** модели экспортированы и описаны в `docs/guide/api.md`; публичный API и typing-тесты
  зелёные; поведение библиотеки не изменилось.

### AR2.2 Диагностический допуск: выбор кандидата и гейт внутри lease

Зависит от `AR2.1`. Ревью, E2.

**Проблема.** Адресная проба сейчас проходит через `Pool.acquire(CandidateQuery)` с
`lifecycles`/`standing` из `OperationRule` (`sync.py::_probe_query`, `pool.py`), а затем второй раз —
через гейт `lease.operation(PROBE_OPERATION)`. `retired`/`cooling` не проходят первый, нужно, чтобы
диагностика обходила оба и ничего больше.

**Предварительно изучить:** `pool.py` (сборка `CandidateQuery`, строка с `rule.lifecycles`),
`lease.py` (`operation()`), `service.py::compile_rule`/`OperationRule`, `sync.py::_resync`.

**Первыми — тесты (красные), `tests/conformance/test_auth_diagnostic_admission.py`:**

- Матрица допуска lifecycle × availability × standing (E2 расширенный) для внутреннего диагностического
  пути: `retired`, `paused`, `cooling` проходят; `quarantined` и `deleted` блокируются по умолчанию с
  `BlockerKind.availability`/`lifecycle` и проходят при явной политике сервиса; `banned`, `frozen`
  проходят (диагностика проверяет сам факт).
- Обычная `sync.refresh`/`refresh_many` **не изменилась**: `retired` и `cooling` по-прежнему
  `LeaseUnavailable` (E2), фоновая сверка не трогает `retired`.
- После диагностики `lifecycle` и `availability` не изменены (retired остаётся retired).
- Сохраняются fencing, TTL аренды, лимит конкурентности и квоты; параллельная диагностика того же
  аккаунта получает `busy`; `actor` и `reason` обязательны (`ValidationFailed` без них).
- Типизированный `Blocker` вместо «нет свободного аккаунта».

**Реализация:** внутренний метод (`space._diagnostic_lease(identity_id, *, actor, reason)`) строит
`CandidateQuery` с `among=[identity_id]` и множествами из `DiagnosticPolicy` сервиса и возвращает lease
с флагом `diagnostic=True`; гейт `lease.operation()` для такого lease использует
`DiagnosticPolicy` вместо `OperationRule.lifecycles/standing` и учитывает те же физические требования
(прокси, квоты). Публичной точки входа очередь здесь не добавляет (её даёт `AR3.1`); аудит — событие
(новый тип `identity.diagnostic.started` с `actor`, `reason`, без секретов; таблица `10-events.md`, контракт
событий) либо существующее событие, если его состава хватает.

- **DoD:** матрица допуска зелёная на обоих бэкендах; обычные пути не изменились; диагностический lease
  не меняет lifecycle/availability; блокировки типизированы; `check.sh` зелёный.

---

## Фаза AR3 — `auth.revalidate()` (L3)

### AR3.1 `auth.revalidate()`: каркас, шаг сессии, структурный результат

Зависит от `AR1.1`, `AR2.2`. Ревью, раздел 6 (L3).

**Первыми — тесты (красные), `tests/conformance/test_auth_revalidate_session.py`:**

- `space.auth.revalidate(identity_id, *, actor, reason, request_id)` для аккаунта с default-сессией:
  `valid` / `invalid` / технический сбой провайдера; провайдер вызван ровно один раз.
- Допуск из `banned` (остаётся `banned`, результат показывает исход `standing`, если пробировался),
  `retired` (остаётся `retired`), `paused/cooling`; `quarantined` без политики → `blocked` с типизированным
  `Blocker`, провайдер не вызван.
- Технический сбой не меняет ни одного региона; прежнее достоверное состояние (`session_invalid`,
  `banned`) сохраняется.
- Результат не содержит секретов; события — без payload сессии.

**Реализация:** публичная модель `AuthRevalidationResult` (frozen): `status` (`completed`, `blocked`,
`in_progress`, `failed`), `session_verdict` (`valid|invalid|missing|failed|not_checked`),
`credential_verdict` (на этом шаге `not_checked`), `applied: Mapping[str, str]` (исход каждого региона:
`applied/unchanged/sticky/pinned/…` — чтобы вызывающий видел, что библиотека НЕ применила),
`blockers: tuple[Blocker, ...]`, `operation_ids`, `retryable`, `challenge_id`. Метод: диагностический
допуск (`AR2.2`) → проверка default-сессии существующей логикой `verify_session` (вердикт —
`Observation`, `AR0.1`) → результат. Состояние `auth` не переводится в `logging_in`. `lifecycle` и
`availability` не меняются. Статусы приложения (`alive`, `dead`, `bad_credentials`) в библиотеку не
попадают. Публичный API: экспорт, `guide/api.md`, surface/typing-тесты.

- **DoD:** шаг сессии работает из любых допустимых состояний, результат структурный, провайдер
  вызывается не чаще одного раза, оба бэкенда.

### AR3.2 `auth.revalidate()`: шаг credential без `logging_in`

Зависит от `AR3.1`. Ревью, 5.1 и раздел 6 (L3).

**Первыми — тесты (красные):**

- Нет пригодной сессии (отсутствует/невалидна) + указанный `credential=(kind, name)`: вызывается
  аутентификатор, успех записывает сессию, `auth → authenticated`, credential помечается verified;
  `standing` не меняется (`banned` остаётся `banned`, результат показывает `applied`), `lifecycle` не
  меняется (`retired` остаётся `retired`).
- Active + ранее отвергнутый (`last_rejected_at`) credential перепроверяется (адресное чтение допускает);
  `missing`, `expired`, `disabled`, `revoked` → `blocked` с типизированным `Blocker`, провайдер не
  вызван.
- `LoginFailure` с кодом: credential помечается отвергнутым, `auth` по вердикту через `Observation`;
  **технический сбой не переводит `auth` в `no_session`** и не теряет `session_invalid`.
- Активный `challenge`: возвращается существующий (`challenge_id`), провайдер не вызывается и новый
  не открывается.
- `LoginChallenge` от провайдера при отсутствии активного challenge: фиксируется challenge по
  существующему механизму `_open_challenge`; решение о способе применения (`Observation(CHALLENGE)`)
  принять тестом; если однозначного варианта нет — `D-NNN`.
- Секреты не попадают в `result`/события.

**Реализация:** выделить из `_login` ядро вызова аутентификатора без постановки `logging_in`
(внутренний `_attempt_login(lease, service, requested_credential, mark_in_progress=False)`; признак «идёт»
обеспечивает claim провайдерной операции). Существующий `login` вызывает ядро с прежним поведением
(`mark_in_progress=True`) — регрессия поведения `login` не допускается (весь набор тестов входа
зелёный). Результат ядра применяется `Observation` + запись сессии в одной транзакции с терминализацией
операции (запись сессии не является переходом графа).

- **DoD:** все сценарии зелёные; `login` не изменился поведенчески; credential-шаг не переводит
  `logging_in`; оба бэкенда.

### AR3.3 `request_id`: идемпотентность и конкурентность

Зависит от `AR1.2`, `AR3.2`. Ревью, 4.4, 5.2, 5.4.

**Контекст.** Живая операция уникальна по `(identity_id, kind)` (`postgres/models.py`, индекс
`uq_ih_provider_operations_live`); поля запроса у операции нет. Нужна nullable-колонка `request_key`
и миграция (`D-012`), отражённая в `MemoryStore`.

**Первыми — тесты (красные):**

- Два параллельных `revalidate` с одним `request_id` → не более одного вызова провайдера; второй
  получает `status="in_progress"` (ожидание — вне очереди).
- Повтор завершённого `request_id` возвращает сохранённый результат без вызова провайдера; тот же
  `request_id` с другими параметрами (`credential`, `identity_id`) → типизированная ошибка (по образцу
  `422` из IETF draft idempotency-key).
- Новый `request_id` после завершения — новая попытка, две исторические записи.
- После `claim_expired` повтор того же `request_id` продолжает попытку, а не блокируется навсегда.
- Anti-flake: конкурентные тесты по 3 прогона.

**Реализация:** миграция + `ProviderOperationRow.request_key` + `enqueue_provider_operation(...,
request_key=...)` для обоих хранилищ; ключи шагов `"{request_id}:session"`, `"{request_id}:credential"`;
уникальность `(space, identity_id, kind, request_key)` для живых и завершённых строк в окне хранения
(retention — по существующей политике очистки завершённых операций). Если у `authenticator` есть
контекст, передавать стабильный provider-idempotency key (необязательно, только если поддерживается).
`request_id` обязателен для публичного `revalidate`.

- **DoD:** гарантия «≤ 1 вызов провайдера на `request_id`» подтверждена тестом на обоих бэкендах;
  миграция вверх/вниз проходит `alembic`-тесты; `SCHEMA_HEAD` и `CHANGELOG` обновлены.

---

## Фаза AR4 — закрытие

### AR4.1 Документация, traceability и финальная сверка

Зависит от `AR0.1`, `AR1.1`, `AR1.2`, `AR2.1`, `AR2.2`, `AR3.1`, `AR3.2`, `AR3.3`.

- Руководство: раздел «Перепроверка аккаунта» (`docs/guide/05-login.md` или отдельная страница) —
  когда `revalidate`, когда `sync.refresh`, что значит `applied/sticky/pinned`, как читать результат,
  как объявить `DiagnosticPolicy`; `docs/guide/04-states.md` — вердикт как наблюдение, защита
  `challenge`; документировать мёртвый `BANNED ∈ confirm_leaving` и почему разбан по наблюдению ручной.
- Памятка потребителю в `docs/upgrade.md` или `docs/integrations.md`: какие обходы `checker_account`
  убираются сразу (`restore_standing` перед пробой), какие после `revalidate` (`activate`,
  `verify_credential`), recovery worker остаётся обязательным.
- Матрица трассировки `docs/auth-revalidation-traceability.md` (требования `AR-R01…`, каждое → ровно
  один pytest node) и тест `tests/test_auth_revalidation_traceability.py` по образцу
  `tests/test_session_first_traceability.py`.
- `CHANGELOG.md`: поведение вердикта, `challenge`, новый API, миграция.
- Полный `scripts/check.sh`; целевые тесты очереди на `MemoryStore` и PostgreSQL; anti-flake 3 полных
  прогона конкурентных тестов; после закрытия — `git worktree list` пуст кроме основного.
- Закрыть исходные материалы: в `docs/isue/auth-revalidation-architecture.md` добавить в шапку
  «Статус: принято частично; решение — `auth-revalidation-review.md`, очередь `AR` (`D-335`)».
- **DoD:** все задачи `AR0.1`–`AR3.3` в статусе `done`/`cancelled`, трассировочный тест зелёный,
  документация и changelog обновлены, `check.sh` зелёный.

---

## 7. Матрица трассировки

| Требование (ревью) | Задачи плана |
|---|---|
| Вердикт провайдера — `Observation`, граф `AUTH` не расширяется | AR0.1 |
| Защита `challenge`/`logging_in` от затирания пробой | AR0.1 |
| Операция не остаётся `running` из-за проекции | AR1.1 |
| Неизвестный исход и recovery не теряют смысл | AR1.2 |
| Диагностический допуск объявляется сервисом | AR2.1, AR2.2 |
| Типизированные блокеры вместо «нет свободного аккаунта» | AR2.1, AR2.2 |
| `auth.revalidate()` и структурный результат | AR3.1, AR3.2 |
| Не менять `lifecycle`/`standing`/`availability` без явной команды | AR3.1, AR3.2 |
| Технический сбой сохраняет последнее достоверное состояние | AR3.1, AR3.2 |
| Идемпотентность по `request_id`, ≤ 1 вызов провайдера | AR3.3 |
| Документация, traceability, закрытие | AR4.1 |
| L4 (политика разбана), RFC B, таблица попыток, правка `login` | вне очереди |
