# Прогресс session-first аутентификации

> Единственный источник правды о статусе очереди `SF*`.
> Обновляется в том же коммите, что код или документация задачи.
> Статусы: `todo`, `in_progress`, `done`, `blocked`, `cancelled`.
> Спецификация: [`session-first-plan.md`](session-first-plan.md).
> Источник требований: [`isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md`](isue/IDENTITY_HUB_SESSION_FIRST_ISSUE.md).
> Архитектурное решение: `D-334` в [`plan/DECISIONS.md`](plan/DECISIONS.md).

## Текущая задача

<!-- Это checkpoint после обрыва сессии. Одновременно активна не более чем одна задача. -->

- **Задача:** SF0.1 — контракт и baseline
- **Начата:** 2026-10-29
- **Репозиторий:** `identity-hub`
- **Шаги:**
  - [ ] Сверить существующее покрытие lifecycle, session-slot и denormalized auth.
  - [ ] Добавить регрессию: `probe` разрешён для `provisioning`, обычные операции — нет.
  - [ ] Добавить контрактные тесты адресного slot и будущего публичного API.
  - [ ] Запустить тесты до production-кода и зафиксировать ожидаемые падения.
  - [ ] Обновить checkpoint и закрыть baseline зелёными проверками.
- **Проверки:** ожидаются после добавления регрессий
- **Заметки для следующей сессии:** начать с существующих unit/conformance fixtures; секреты в assertions не выводить.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| SF0.1 | Контракт и baseline | — | in_progress |  | падающие регрессии до production-кода |
| SF1.1 | Lifecycle в `OperationRule` | SF0.1 | todo |  | `probe`: provisioning + ready; retired запрещён |
| SF1.2 | Адресный session-slot в `ProbeContext` | SF0.1 | todo |  | default source-compatible |
| SF2.1 | Публичный `auth.verify_session` | SF1.1, SF1.2 | todo |  | один provider call, metadata verdict |
| SF2.2 | Атомарный `auth.adopt_session` | SF2.1 | todo |  | canonical default + auth + optional activate |
| SF2.3 | Гонки, fencing и rollback | SF2.2 | todo |  | Memory/PostgreSQL, anti-flake x3 |
| SF3.1 | Публичный контракт и документация | SF2.3 | todo |  | guides, CHANGELOG, typing, traceability |
| SF4.1 | Точечные проверки checker | SF3.1 | todo |  | отдельный commit в checker |
| SF4.2 | Batch parity `run_checks` | SF4.1 | todo |  | session-first во всех entry points или удаление legacy |
| SF5.1 | Cross-repo приёмка и выпуск | SF3.1, SF4.2 | todo |  | установленные артефакты, оба полных gate |

**Итого:** 0 / 10 done

## Журнал сессий

2026-10-29 · план · подтверждены три пробела: lifecycle gate блокирует probe на provisioning,
native probe не принимает конкретный session-slot, а consumer вынужден копировать session в
default и повторно проверять её ради глобального auth. Создана очередь `SF0`-`SF5`; зафиксированы
canonical default, один provider call, атомарная финальная транзакция, явная активация и
cross-repo migration. Реализация не начата. Дальше `SF0.1`.
