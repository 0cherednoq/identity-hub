# Прогресс session-first аутентификации

> Единственный источник правды о статусе очереди `SF*`.
> Обновляется в том же коммите, что код или документация задачи.
> Статусы: `todo`, `in_progress`, `done`, `blocked`, `cancelled`.
> Спецификация: [`session-first-plan.md`](session-first-plan.md).
> Источник требований: [`isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md`](isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md).
> Архитектурное решение: `D-334` в [`plan/DECISIONS.md`](plan/DECISIONS.md).

## Текущая задача

<!-- Это checkpoint после обрыва сессии. Одновременно активна не более чем одна задача. -->

- **Задача:** SF5.1 — cross-repo приёмка и выпуск
- **Начата:** 2026-10-30
- **Репозиторий:** `checker_account` + checkpoint в `identity-hub`
- **Шаги:**
  - [ ] Выполнить полный gate Identity Hub и checker.
  - [ ] Собрать wheel/sdist Identity Hub и проверить checker на установленном wheel.
  - [ ] Повторить MemoryStore/PostgreSQL и ARMGS cookie-only/token+cookies acceptance.
  - [ ] Проверить один provider call и отсутствие secret-bearing event/result/log полей.
  - [ ] Обновить версию, release notes и dependency/lock checker.
  - [ ] Закрыть исходную issue ссылками на тесты и commit hashes.
- **Проверки:** ожидаются финальные gates и installed-artifact matrix
- **Заметки для следующей сессии:** SF4.2 — checker `e533a5d`, IH stale-provider fence `1dd0feb`;
  checker 117 passed на editable IH, IH session-first 42 passed Memory/PostgreSQL, mypy/ruff зелёные.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| SF0.1 | Контракт и baseline | — | done |  | 55 старых тестов зелёные; новые контракты ожидаемо красные |
| SF1.1 | Lifecycle в `OperationRule` | SF0.1 | done |  | 66 targeted tests; mypy src; ruff |
| SF1.2 | Адресный session-slot в `ProbeContext` | SF0.1 | done |  | named/default/cookie-only + typing |
| SF2.1 | Публичный `auth.verify_session` | SF1.1, SF1.2 | done |  | 39 targeted tests; Memory/PostgreSQL |
| SF2.2 | Атомарный `auth.adopt_session` | SF2.1 | done |  | 10 Memory + 10 PostgreSQL; rollback/typing/ruff |
| SF2.3 | Гонки, fencing и rollback | SF2.2 | done |  | 36 full; concurrency 16 × 3; mypy/ruff |
| SF3.1 | Публичный контракт и документация | SF2.3 | done |  | 43 docs/public + 67 conformance; mypy/ruff |
| SF4.1 | Точечные проверки checker | SF3.1 | done | `a222b89` (checker) | 21 targeted + 115 full; ruff |
| SF4.2 | Batch parity `run_checks` | SF4.1 | done | `e533a5d` (checker), `1dd0feb` | 117 full; concurrent worker/provider call |
| SF5.1 | Cross-repo приёмка и выпуск | SF3.1, SF4.2 | in_progress |  | установленные артефакты, оба полных gate |

**Итого:** 9 / 10 done

## Журнал сессий

2026-10-29 · план · подтверждены три пробела: lifecycle gate блокирует probe на provisioning,
native probe не принимает конкретный session-slot, а consumer вынужден копировать session в
default и повторно проверять её ради глобального auth. Создана очередь `SF0`-`SF5`; зафиксированы
canonical default, один provider call, атомарная финальная транзакция, явная активация и
cross-repo migration. Реализация не начата. Дальше `SF0.1`.

2026-10-29 · SF0.1 · добавлены исполняемые контракты lifecycle и будущего API. До production-кода:
unit lifecycle — 2 ожидаемых падения, `sync.refresh` на provisioning — 2 ожидаемых падения
(MemoryStore/PostgreSQL), public API — 1 ожидаемое падение, mypy — 4 ожидаемые ошибки символов;
соседний baseline — 55 passed, 4 deselected. Существующий PostgreSQL-тест подтвердил, что только
`auth/default` денормализует глобальный auth. Дальше `SF1.1`.

2026-10-29 · SF1.1 · lifecycle добавлен в `OperationRule`/`RuleFilter`, candidate query и execution
gate используют одну политику. Автоматический `probe` разрешает `provisioning` и `ready`, но не
`retired`; обычные операции сохраняют default `{ready}`. Проверки: 66 passed на unit + conformance
(MemoryStore/PostgreSQL), `mypy src` и targeted `ruff` зелёные. Дальше `SF1.2`.

2026-10-29 · SF1.2 · `ProbeContext` получил read-only `session_slot`; `session()` читает выбранный
slot, а явный аргумент сохраняет чтение другого слота. Внутренняя проба принимает targeted slot,
обычный sync остаётся на `default`. Проверены named slot, явный default и cookie-only
`BrowserSession`: 49 targeted tests, mypy 169 файлов и ruff зелёные. Дальше `SF2.1`.

2026-10-29 · SF2.1 · добавлены публичные DTO и `auth.verify_session`. Метод проверяет ровно один
slot под lease/provider claim, выполняет один provider call вне DB-транзакции и применяет validity
metadata с повторной проверкой fence и session revision. Non-default не меняет глобальный auth;
invalid `default` переводит его в `session_invalid`; lifecycle не меняется.
Missing/invalid/technical outcomes покрыты на MemoryStore и PostgreSQL. Проверки: 39 passed,
`mypy` 169 файлов и ruff зелёные; typing baseline остался красным только для будущего adoption.
Дальше `SF2.2`.

2026-10-29 · SF2.2 · добавлен общий targeted auth-probe primitive и публичный
`auth.adopt_session`. Проверенный payload запечатывается до финальной транзакции; source validity,
canonical `default`, глобальный auth, optional lifecycle и provider outcome применяются одним
commit. Повтор не создаёт новую default history; source=`default` не заменяет себя. Инъекционный
сбой после default write подтверждает полный rollback. Проверки: 10 Memory + 10 PostgreSQL,
public contract, полный typing и ruff зелёные. PostgreSQL testcontainer дважды терял соединение
при параллельном запуске, отдельный serial gate прошёл. Дальше `SF2.3`.

2026-10-29 · SF2.3 · покрыты replacement source во время provider call, конкурентные adoption
разных slots, stale/late expected revision, cancellation, expired claim и revoked lease. Ни один
late result не создаёт default и не меняет auth/lifecycle; последовательные победители оставляют
coherent default последнего provider verdict. Secret-bearing текст отсутствует в result, log и
event payload. Полная матрица: 36 passed; concurrency subset: 16 passed три раза подряд на
MemoryStore/PostgreSQL; mypy 171 файл и ruff зелёные. Дальше `SF3.1`.

2026-10-29 · SF3.1 · для интегратора добавлен session-first how-to с exact credential fallback,
reference outcomes/lifecycle/canonical default, разграничение сетевых `auth.*` и ручных
`secrets.*`, concurrency и audit semantics. Удалено устаревшее ограничение provisioning probe.
Обновлены API guide, states, limitations, CHANGELOG, typing/public exports и отдельная матрица
`session-first-traceability.md`. При сверке с D-334 исправлен invalid `default`: теперь обе
сетевые операции атомарно ставят глобальный `session_invalid`. Проверки: 43 docs/public,
67 conformance на MemoryStore/PostgreSQL, полный typing и ruff зелёные. Дальше `SF4.1`.

2026-10-30 · SF4.1 · ручная exact-slot проверка переведена на `auth.verify_session`, а account
session-first — на последовательный `auth.adopt_session(..., activate=True)`. Удалены раскрытие
session в store, прямой `verify_armgs_session`, ручное копирование в `default`, отдельные
verify/reject, `activate -> sync.refresh` и broad exception. INVALID переходит к точному паролю,
FAILED даёт retryable UNKNOWN без password fallback, CHALLENGED сохраняется отдельно. Проверены
cookie-only, точная адресация нескольких slots и немедленная видимость через глобальный auth:
21 targeted, весь checker suite 115 passed, ruff зелёный. Checker commit `a222b89`. Дальше `SF4.2`.

2026-10-30 · SF4.2 · `run_checks`, FlexiQ worker, interactor и `check-file` направлены через единый
`check_one`: ограниченная страница provisioning/ready кандидатов, session-first до пароля и
без ложного verdict для занятого/stale identity. Batch передаёт revision выбранной строки;
session API повторно сверяет её после ожидания lease и до provider call, поэтому конкурентный
worker не делает второй сетевой вызов. Добавлены cookie-only + wrong-password и concurrent batch
тесты. Проверки: checker 117 passed, ruff; IH session-first 42 passed на Memory/PostgreSQL,+mypy/ruff. Коммиты: checker `e533a5d`, IH `1dd0feb`. Дальше `SF5.1`.
