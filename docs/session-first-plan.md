# Identity Hub: план очереди `SF` для session-first аутентификации

Источник требований: [`isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md`](isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md).
Статус задач: [`session-first-progress.md`](session-first-progress.md).
Архитектурное решение: `D-334` в [`plan/DECISIONS.md`](plan/DECISIONS.md).

План предназначен для разработчика Identity Hub и интегратора checker. Результат очереди: checker
проверяет конкретный session-slot штатным API Identity Hub, принимает живую сессию как
`auth=authenticated` за один сетевой вызов и удаляет прямой доступ к секрету и обход
`activate -> sync.refresh`.

## Результат реализации

После закрытия очереди доступны две разные операции:

```python
checked = await space.auth.verify_session(
    identity_id,
    slot="cookie-1",
    actor="checker",
)

adopted = await space.auth.adopt_session(
    identity_id,
    slot="cookie-1",
    actor="checker",
    activate=True,
)
```

`verify_session` проверяет указанный слот и сохраняет вердикт, но не делает сессию активной.
`adopt_session` проверяет указанный слот, копирует подтверждённую сессию в канонический
`default`, выставляет `auth/default=authenticated` и при явном `activate=True` переводит
`provisioning -> ready`.

## Границы и инварианты

1. `default` остаётся единственным каноническим активным session-slot. Новое поле
   `active_session_slot` и миграция схемы не вводятся.
2. `verify_session` и `adopt_session` выполняют ровно один вызов provider probe на одну попытку.
   Реализация через последовательность публичных `verify_session`, `replace_session` и
   `sync.refresh` запрещена.
3. Сетевой вызов выполняется вне долгой транзакции. Его локальный результат применяется одной
   финальной транзакцией с проверкой lease fence, provider claim и ревизии исходного слота.
4. Успешное принятие non-default слота копирует payload в `default`. Исходный слот сохраняется
   для аудита и истории. Оба слота получают `last_verified_at` одной логической командой.
5. Доказанная невалидность non-default слота не меняет глобальный `auth`. Невалидность
   канонического `default` переводит `auth/default` в `session_invalid`.
6. Техническая ошибка, timeout, потеря lease или истёкший provider claim не помечают сессию
   rejected и не меняют lifecycle/auth.
7. Операции разрешены для `provisioning` и `ready`. `retired` не восстанавливается автоматически:
   вызывающий сначала выполняет `identities.activate(..., reason=...)`.
8. `activate=False` может оставить успешно аутентифицированный identity в `provisioning`.
   Это соответствует существующему пути `auth.login`, после которого активация остаётся явной.
9. `secrets.verify_session` и `secrets.reject_session` сохраняются как команды ручной пометки без
   сетевой проверки. Документация должна явно отличать их от `auth.verify_session`.
10. Секреты, cookies, proxy URL и provider exception text не попадают в события, операции,
    результаты API или логи.
11. Публичные методы аддитивны. Существующие `auth.login`, `sync.refresh` и контракты probe без
    новых аргументов сохраняют поведение.
12. Строгая проверка конкретного password credential при уже `authenticated` identity не входит
    в эту очередь. Текущий `auth.login(credential=(kind, name))` остаётся точным fallback для
    `no_session`/`session_invalid`. Отдельный контракт re-auth описан в отложенных задачах.

## Публичный контракт

Рабочие имена типов фиксируются до реализации в `SF0.1` и меняются только через запись решения.

```python
class SessionVerificationOutcome(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    CHALLENGED = "challenged"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionVerificationResult:
    identity_id: UUID
    slot: str
    outcome: SessionVerificationOutcome
    state: AuthState
    code: str | None = None
    retryable: bool = False


class SessionAdoptionOutcome(StrEnum):
    ADOPTED = "adopted"
    INVALID = "invalid"
    CHALLENGED = "challenged"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionAdoptionResult:
    identity_id: UUID
    source_slot: str
    active_slot: str
    outcome: SessionAdoptionOutcome
    auth: AuthState
    lifecycle: Lifecycle
    code: str | None = None
    retryable: bool = False
```

Методы:

```python
class Auth:
    async def verify_session(
        self,
        identity_id: UUID,
        slot: str = "default",
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> SessionVerificationResult: ...

    async def adopt_session(
        self,
        identity_id: UUID,
        slot: str = "default",
        *,
        actor: str,
        activate: bool = False,
        expected_revision: int | None = None,
    ) -> SessionAdoptionResult: ...
```

Правила ошибок:

| Ситуация | Результат |
|---|---|
| Сервис не объявил `probes.auth` | `ValidationFailed` до lease/provider call |
| Слот отсутствует, revoked или expired | `INVALID` с безопасным стабильным `code`; сеть не вызывается |
| Provider доказал невалидность | `INVALID`; слот помечен rejected |
| Provider потребовал challenge | `CHALLENGED`; слот не принят |
| Timeout или технический сбой | `FAILED`, `retryable=True`; состояние не меняется |
| Identity занят | существующий `LeaseUnavailable` |
| Исходный слот заменён во время probe | `ConcurrentModification`; результат probe не применяется |
| Потерян lease или provider claim | существующая ошибка fencing/claim; состояние не меняется |
| `retired` | `OperationNotAllowed` с blocker `lifecycle` |

## Внутренний поток

```text
Auth.verify_session / Auth.adopt_session
    -> проверить service, slot и lifecycle
    -> взять служебный exclusive lease среди [identity_id]
    -> зафиксировать revision/fingerprint исходного слота
    -> создать provider operation claim
    -> вызвать probes.auth с ProbeContext.session_slot = slot
    -> подготовить payload default вне DB-транзакции
    -> одна финальная транзакция:
         проверить lease fence и claim
         проверить revision/fingerprint исходного слота
         применить verdict исходного слота
         [adopt] записать default и его verified metadata
         [adopt] применить auth/default
         [adopt, activate] применить lifecycle=ready
         завершить provider operation
         записать безопасные события
    -> освободить lease
```

Нельзя держать транзакцию открытой во время сетевого вызова. Нельзя применять ответ provider,
если текущая ревизия исходного слота отличается от проверенной.

## Очередь реализации

### SF0.1 Зафиксировать контракт и baseline

Цель: превратить подтверждённые расхождения в исполняемые тесты до изменения production-кода.

- Добавить unit-тест admissibility: правило `probe` должно отличать `provisioning`, `ready` и
  `retired`.
- Добавить conformance-регрессию, которая воспроизводит текущий
  `OperationNotAllowed: lifecycle: provisioning` до вызова probe.
- Добавить тест текущей slot-модели: `auth/cookie-1=authenticated` не денормализует глобальный
  auth, `auth/default=authenticated` денормализует.
- Добавить API/typing-тесты будущих имён `verify_session`, `adopt_session`, DTO и enum.
- Зафиксировать baseline существующих тестов `sync`, `auth`, session validity и provider
  operations на MemoryStore и PostgreSQL.
- Записать команды, число тестов и ожидаемые причины падения в progress checkpoint.

**DoD:** старое поведение защищено регрессиями, новые контрактные тесты падают только по причине
отсутствующего API или lifecycle policy, baseline записан в progress.

### SF1.1 Сделать lifecycle частью `OperationRule`

Зависит от `SF0.1`.

- Добавить `lifecycles: frozenset[str]` в `OperationRule`, default `{ready}`.
- Добавить lifecycle в `RuleFilter` и `compile_rule()`.
- Перевести `admissibility.evaluate()` с безусловного `READY` на lifecycle из правила.
- Автоматическое правило `probe` объявить для `{provisioning, ready}`.
- Передавать те же lifecycles в `Pool.candidate_query(for_operation=...)` и служебный
  `_probe_query`, чтобы выборка и execution gate не расходились.
- Валидировать неизвестные lifecycle при построении `Service`.
- Обновить unit и conformance-тесты на MemoryStore/PostgreSQL. Проверить, что обычные операции
  по-прежнему запрещены в `provisioning`.

**DoD:** `sync.refresh` может выполнить probe на `provisioning`, не может на `retired`, правила
обычных операций не ослаблены, оба Store дают одинаковый результат.

### SF1.2 Добавить адресный session-slot в `ProbeContext`

Зависит от `SF0.1`.

- Добавить read-only `session_slot` в `ProbeContext`.
- Изменить `ProbeContext.session` на `slot: str | None = None`: `None` означает текущий
  `session_slot`, явная строка читает указанный слот.
- Обычный `sync.refresh` создаёт context с `session_slot="default"`.
- Внутренний targeted probe создаёт context с выбранным slot. Существующий адаптер, который
  вызывает `ctx.session()` без аргумента, проверяет выбранную сессию без изменения кода.
- Сохранить поддержку явного `ctx.session("other")` и проверку schema конкретного слота.
- Добавить adapter-context и typing-тесты, включая cookie-only session.

**DoD:** один и тот же `probes.auth` корректно проверяет default и именованный слот; старый вызов
`ctx.session()` остаётся source-compatible.

### SF2.1 Реализовать `auth.verify_session`

Зависит от `SF1.1`, `SF1.2`.

- Добавить публичные enum/DTO и экспорт из корневого пакета.
- Реализовать служебный lease и provider claim по образцу login/probe, без раскрытия session
  через публичный `secrets.reveal`.
- До сети отклонять отсутствующий, revoked и expired slot.
- После provider verdict атомарно завершать claim и выставлять validity metadata исходного слота.
- При invalid default применять `auth/default=session_invalid`; invalid non-default не меняет
  глобальный auth.
- Timeout и технические исключения классифицировать безопасным кодом, не ставить rejected.
- Добавить события на базе существующего семейства `identity.session.updated` и provider
  operation `verify_session:<slot>` без payload секрета.
- Добавить Memory/PostgreSQL parity, fencing, timeout и audit-тесты.

**DoD:** checker может проверить конкретный слот одним вызовом IH; результат и metadata
согласованы; технический сбой не создаёт ложный reject.

### SF2.2 Реализовать атомарный `auth.adopt_session`

Зависит от `SF2.1`.

- Переиспользовать внутренний verifier из `SF2.1`, а не вызывать публичный `verify_session`.
- Зафиксировать revision и value fingerprint исходного слота до сети; перепроверить их в
  финальной транзакции.
- Для non-default подготовить зашифрованный payload с AAD `default` вне транзакции и записать
  его только после успешного provider verdict.
- В одной транзакции: проверить fence/claim, принять source slot, заменить default, пометить оба
  слота verified, применить `auth/default=authenticated`, опционально применить
  `lifecycle=ready`, завершить provider operation и записать события.
- Для `slot="default"` не создавать новую session history row без необходимости.
- Сделать повтор с тем же revision/fingerprint идемпотентным по состоянию. Повтор может снова
  проверить provider, но не должен плодить идентичные default-записи и события.
- Не восстанавливать `retired`; возвратить lifecycle blocker.
- Проверить aggregate revision: одна логическая команда повышает её не более одного раза.

**DoD:** valid `cookie-1` на provisioning identity за один provider call становится
`default + authenticated`, а с `activate=True` также `ready`; никакое частичное состояние не
остаётся при сбое финальной транзакции.

### SF2.3 Закрыть гонки и матрицу отказов

Зависит от `SF2.2`.

- Гонка `replace_session(source)` во время provider call: adoption отклонён, новый payload не
  считается проверенным.
- Гонка двух adoption разных слотов: победитель определён fence/claim/revision; глобальный auth
  и default соответствуют одному результату.
- Потеря lease, истечение claim и отмена задачи после provider call не применяют поздний ответ.
- Rollback на ошибке записи default оставляет source metadata, auth и lifecycle неизменными.
- `expected_revision` проверяется до provider call и повторно на commit boundary.
- Прогнать concurrency-наборы MemoryStore и PostgreSQL три раза.

**DoD:** детерминированная матрица гонок зелёная на обоих Store; late provider result ничего не
пишет; секреты отсутствуют в исключениях, событиях и логах.

### SF3.1 Завершить публичный контракт и документацию

Зависит от `SF2.3`.

- Обновить `docs/guide/05-login.md`, `docs/guide/04-states.md`, `docs/guide/api.md` и
  `docs/guide/limitations.md`.
- Явно различить сетевые `auth.verify_session`/`auth.adopt_session` и ручные metadata-команды
  `secrets.verify_session`/`secrets.reject_session`.
- Документировать canonical default, lifecycle matrix, invalid/technical outcomes, аудит и
  поведение `retired`.
- Добавить пример session-first orchestration с fallback на
  `auth.login(credential=(kind, name))`.
- Обновить `CHANGELOG.md`, public API tests, typing examples и traceability.

**DoD:** интегратор реализует session-first без чтения приватного кода и без раскрытия session;
документация содержит success, invalid, technical failure и concurrent replacement.

### SF4.1 Мигрировать точечные проверки checker

Зависит от `SF3.1`. Выполняется в репозитории checker; hash его коммита записывается в progress.

- Заменить ручной `check_session(identity_id, slot)` на `space.auth.verify_session`.
- Заменить `_session_login` на последовательный вызов `auth.adopt_session` по выбранным слотам,
  затем fallback `auth.login(credential=...)`.
- Удалить из store прямые `secrets.reveal`, `verify_armgs_session`, копирование в default,
  `activate -> sync.refresh` и broad `except Exception` вокруг auth flip.
- Сохранить ARMGS probe как adapter `Service.probes.auth`, включая cookie-only.
- Явно разделить методы checker: account liveness, exact session check и exact credential login.
- Проверить: живая cookie не трогает пароль; мёртвая cookie ведёт к точному credential;
  технический сбой даёт retryable UNKNOWN; глобальный filter видит аккаунт сразу.

**DoD:** точечный session-first checker не раскрывает session за пределы IH, делает один сетевой
вызов на slot и не содержит старого обхода.

### SF4.2 Устранить расхождение batch `run_checks`

Зависит от `SF4.1`. Выполняется в репозитории checker.

- Зафиксировать решение для `CheckAccountsInteractor.execute -> run_checks`: либо направить batch
  на тот же `check_one` orchestration, либо официально удалить/deprecate legacy entry point.
- Не добавлять `adopt_session_many` до измеренного требования: cross-process exclusivity уже
  обеспечивает служебный lease каждого одиночного вызова.
- При сохранении batch выбрать кандидатов ограниченной страницей, обрабатывать занятые identity
  без ложного verdict и соблюдать `limit`.
- Добавить тест, где batch получает provisioning identity с живым `cookie-N` и неверным паролем:
  результат ALIVE, password authenticator не вызван.
- Добавить тест конкурентных batch workers: один identity не получает два финальных verdict.

**DoD:** все публичные entry points checker имеют одинаковую session-first семантику либо legacy
entry point удалён с migration note.

### SF5.1 Финальная приёмка и выпуск

Зависит от `SF3.1`, `SF4.2`.

- Выполнить полный `scripts/check.sh` Identity Hub и профильные тесты checker.
- Повторить acceptance matrix на установленном артефакте, а не только editable checkout.
- Проверить MemoryStore, PostgreSQL и ARMGS adapter с cookie-only и token+cookies.
- Проверить отсутствие двойного provider call счётчиком adapter-а.
- Проверить события/provider operations на отсутствие secret-bearing полей.
- Обновить версию, release notes и compatibility requirement checker только после зелёной
  cross-repo проверки.
- Закрыть исходную issue ссылками на тесты и оба commit hash.

**DoD:** полный gate обоих репозиториев зелёный; acceptance criteria issue подтверждены тестами;
checker зависит от выпущенной версии с новым API и не содержит workaround.

## Порядок и зависимости

```text
SF0.1 -> SF1.1 --+
   |             +-> SF2.1 -> SF2.2 -> SF2.3 -> SF3.1 -> SF4.1 -> SF4.2 -> SF5.1
   +-> SF1.2 ----+
```

Критический путь библиотеки заканчивается на `SF3.1`. `SF4.*` проверяет реального потребителя и
не должен блокировать подготовку library changes, но блокирует выпуск совместимой версии checker.

## Проверки

Targeted gate после `SF1.*`:

```bash
uv run pytest -q tests/unit/test_admissibility.py tests/unit/test_adapter_contexts.py
uv run pytest -q tests/conformance/test_probe_operations.py tests/conformance/test_auth_sync_links_api.py
uv run mypy src tests/typing
uv run ruff check src tests
```

Targeted gate после `SF2.*`:

```bash
uv run pytest -q tests/conformance -k "session and (verify or adopt or probe)"
uv run pytest -q tests/pg/test_sync.py tests/pg/test_logins.py tests/pg/test_secrets_admin.py
uv run pytest -q tests/pg -k "session and (race or fencing or provider)"
```

Финальный gate Identity Hub:

```bash
bash scripts/check.sh > .check.log 2>&1; rc=$?; tail -3 .check.log; [ $rc -eq 0 ]
```

Профильный gate checker:

```bash
uv run pytest -q \
  apps/checker/tests/test_targeting.py \
  apps/checker/tests/test_cookie_sessions.py \
  apps/checker/tests/test_armgs_authenticator.py
```

## Сохранение прогресса

Единственный источник статуса очереди: [`session-first-progress.md`](session-first-progress.md).
Каждая рабочая сессия выполняет следующий протокол:

1. Прочитать checkpoint, таблицу задач и последние записи журнала.
2. Прочитать `D-334`, раздел текущей задачи и связанные исходники/тесты.
3. Если есть `in_progress`, продолжить с первого незакрытого шага. Иначе взять первую `todo` с
   закрытыми зависимостями.
4. Перед кодом записать в checkpoint 3-8 проверяемых шагов и поставить `in_progress`.
5. После каждого завершённого шага поставить `[x]` и обновить заметку для следующей сессии.
6. Для `SF0.*` и исправлений сначала написать падающий regression test.
7. Закрыть задачу только после её DoD и зелёных проверок. Код, тесты, документация и progress
   одной задачи попадают в один коммит.
8. Для `SF4.*` записывать отдельный commit hash checker в колонку «Коммит» или «Примечание».
9. `blocked` допустим только при внешней причине. Причина, доказательства и следующий безопасный
   шаг остаются в checkpoint и таблице.

Допустимые статусы: `todo`, `in_progress`, `done`, `blocked`, `cancelled`. Одновременно активна
не более чем одна задача. Рекомендуемые closing commits: `fix(SF<id>): ...` для дефектов,
`feat(SF<id>): ...` для нового API, `docs(SF<id>): ...` для чистой документации.

## Отложенные задачи

| Идея | Условие возврата | Почему не входит сейчас |
|---|---|---|
| `auth.reauthenticate(identity_id, credential=...)` для строгой проверки пароля при живой session | Появился UI/процесс, которому нужен verdict именно пароля, а не живости identity | Требует определить, заменяет ли новый login активную session и может ли provider инвалидировать старую |
| `active_session_slot` вместо canonical `default` | Нужны несколько одновременно активных auth-контекстов в фильтрах и операциях | Требует миграции, изменения денормализации, read-схемы, фильтров и admissibility |
| `adopt_session_many` | Измерение показало, что одиночные leases/provider calls ограничивают batch throughput | Усложняет отчёт, retries и cross-process claims без необходимости для корректности |
| Автоматическое восстановление `retired` | Владелец явно разрешил снимать retired без отдельной причины | Сейчас нарушает lifecycle audit и существующий guard перехода `retired -> ready` |
