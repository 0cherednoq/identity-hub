# Прогресс session-first аутентификации

> Единственный источник правды о статусе очереди `SF*`.
> Обновляется в том же коммите, что код или документация задачи.
> Статусы: `todo`, `in_progress`, `done`, `blocked`, `cancelled`.
> Спецификация: [`session-first-plan.md`](session-first-plan.md).
> Источник требований: [`isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md`](isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md).
> Архитектурное решение: `D-334` в [`plan/DECISIONS.md`](plan/DECISIONS.md).

## Текущая задача

<!-- Это checkpoint после обрыва сессии. Одновременно активна не более чем одна задача. -->

- **Задача:** —
- **Начата:** —
- **Репозиторий:** —
- **Шаги:**
  - [ ] —
- **Проверки:** —
- **Заметки для следующей сессии:** следующий доступный этап — `SF2.2`; typing baseline теперь ожидаемо падает только на `adopt_session`.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| SF0.1 | Контракт и baseline | — | done |  | 55 старых тестов зелёные; новые контракты ожидаемо красные |
| SF1.1 | Lifecycle в `OperationRule` | SF0.1 | done |  | 66 targeted tests; mypy src; ruff |
| SF1.2 | Адресный session-slot в `ProbeContext` | SF0.1 | done |  | named/default/cookie-only + typing |
| SF2.1 | Публичный `auth.verify_session` | SF1.1, SF1.2 | done |  | 39 targeted tests; Memory/PostgreSQL |
| SF2.2 | Атомарный `auth.adopt_session` | SF2.1 | todo |  | canonical default + auth + optional activate |
| SF2.3 | Гонки, fencing и rollback | SF2.2 | todo |  | Memory/PostgreSQL, anti-flake x3 |
| SF3.1 | Публичный контракт и документация | SF2.3 | todo |  | guides, CHANGELOG, typing, traceability |
| SF4.1 | Точечные проверки checker | SF3.1 | todo |  | отдельный commit в checker |
| SF4.2 | Batch parity `run_checks` | SF4.1 | todo |  | session-first во всех entry points или удаление legacy |
| SF5.1 | Cross-repo приёмка и выпуск | SF3.1, SF4.2 | todo |  | установленные артефакты, оба полных gate |

**Итого:** 4 / 10 done

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
metadata с повторной проверкой fence и session revision; глобальный auth/lifecycle не меняются.
Missing/invalid/technical outcomes покрыты на MemoryStore и PostgreSQL. Проверки: 39 passed,
`mypy` 169 файлов и ruff зелёные; typing baseline остался красным только для будущего adoption.
Дальше `SF2.2`.
