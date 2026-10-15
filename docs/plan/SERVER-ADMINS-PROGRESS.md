# Прогресс исправлений по ишью server-admins

> Единственный источник правды о статусе очереди `S` (ишью 01–08 из `docs/isue/server-admins/`, аудит `docs/isue/09-assessment.md`; запросы владельца `D-311`–`D-314`).
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`SERVER-ADMINS-PLAN.md`](SERVER-ADMINS-PLAN.md).
> Требования: [`../isue/09-assessment.md`](../isue/09-assessment.md). Протокол: [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint для продолжения после обрыва. -->

- **Задача:** —
- **Начата:** —
- **Шаги:**
  - [ ] —
- **Заметки для следующей сессии:** —

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| S0.1 | Измерение `code` у `logins.failed` | — | done |  | ишью 02, P0 |
| S0.2 | `deleted` терминален для наблюдений | — | done |  | ишью 04, P0 |
| S1.1 | `LoginFailure`: код отказа в карточке, событии и операции | S0.1 | done |  | ишью 01, P1 |
| S1.2 | `LoginFailure.fault`: репутация при входе одной транзакцией | S0.2, S1.1 | done |  | ишью 07, P1 |
| S2.1 | `Fault.until` | S1.2 | done |  | ишью 03, P2 |
| S2.2 | Временный бан | S2.1 | done |  | ишью 03; запрос владельца, `D-311` |
| S3.1 | Документировать замену credentials и `cool_down` из карантина | — | done |  | ишью 05, 06 |
| S3.2 | Явный сброс кода входа при замене credentials | S1.1, S3.1 | done |  | ишью 05, P2 |
| S4.1 | Финальная сверка | S0.1, S0.2, S1.1, S1.2, S2.1, S2.2, S3.1, S3.2 | done |  | закрытие |
| S5.1 | Режим `secret_storage="plaintext"`: схема, запись, чтение | — | done |  | запрос владельца, `D-313`; миграция |
| S5.2 | Перевод хранения, предупреждения и документация | S5.1 | done |  | запрос владельца, `D-313` |
| S6.1 | Модели, нормализация и каркас `dump`/`load` | — | done |  | запрос владельца, `D-314` |
| S6.2 | Браузеры: `dump`/`load` и методы аренды | S6.1 | todo |  | `D-314` |
| S6.3 | HTTP-клиенты: `dump`/`load` | S6.1 | todo |  | `D-314` |
| S6.4 | Файлы и автоопределение | S6.1, S6.2 | todo |  | `D-314` |
| S6.5 | Отпечаток: `dump`/`load` | S6.1, S6.2 | todo |  | `D-314` |
| S6.6 | Интеграция с хабом и документация | S5.2, S6.1, S6.2, S6.3, S6.4, S6.5 | todo |  | `D-314`; закрытие S5–S6 |

**Итого:** 12 / 17 done

## Журнал сессий
2026-10-15 · S0.1 · done · code во всех login.failed и legacy fallback rollup
2026-10-15 · S0.2 · done · deleted остаётся терминальным вне явного override
2026-10-15 · S1.1 · done · типизированный LoginFailure и безопасный код в карточке, событии и операции
2026-10-15 · S1.2 · done · общая атомарная политика Fault для login и provider operations
2026-10-15 · S2.1 · done · Fault.until валидируется и передаётся только для поддерживаемого standing
2026-10-15 · S2.2 · done · временный banned снимается по until, бессрочный остаётся
2026-10-15 · S3.1 · done · прежняя замена credentials сохраняет auth и session; карантин не ослабляется
2026-10-15 · S3.2 · done · replace_credentials(reset_auth=True) атомарно очищает прошлый отказ входа
2026-10-15 · S4.1 · done · финальная документация и traceability; 2047 passed; anti-flake 3×330 passed
2026-10-15 · S5.1 · done · dual-format encrypted/plaintext storage, migration 0033, mixed reads and tests
2026-10-15 · S5.2 · done · batch conversion, CLI, warnings, audit and 1000-row round trip
2026-10-15 · S6.1 · done · canonical models, RFC OTP, JSON namespaces and per-slot session schemas
