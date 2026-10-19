# Прогресс resource management

> Единственный источник правды о статусе очереди `RM*`.
> Обновляется в том же коммите, что код или документация задачи.
> Статусы: `todo`, `in_progress`, `done`, `blocked`, `cancelled`.
> Спецификация: [`resource-management-plan.md`](resource-management-plan.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint после обрыва сессии. -->

- **Задача:** —
- **Начата:** —
- **Шаги:**
  - [ ] —
- **Заметки для следующей сессии:** Очередь RM0–RM4 завершена. Полный gate выполнен в штатном
  sharded-режиме; `demo/` не входит в изменения и не должен попадать в коммит.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| RM0.1 | Baseline и публичный контракт | — | done |  | 8 reserve-тестов зелёные; RED: отсутствует `EligibleCapacity` |
| RM1.1 | `EligibleCapacity` и базовый `eligible_count()` | RM0.1 | done |  | 13 targeted passed; Ruff/mypy green |
| RM1.2 | Паритет фильтров ёмкости и аренды | RM1.1 | done |  | 39 conformance + 1 PG Select; Ruff/mypy green |
| RM1.3 | API guide и интеграционный рецепт | RM1.2 | done |  | 10 public API tests passed |
| RM2.1 | Все причины отказа аренды | — | done |  | 5/5 enum values; 11 public API tests passed |
| RM2.2 | Удалить устаревший обход типизации | — | done |  | mypy contract fixed; 14 tests passed |
| RM3.1 | Событие `pool.capacity.ok` | RM1.2 | done |  | 21 event pipeline tests passed |
| RM3.2 | Точки пробуждения governor | RM3.1 | done |  | 35 targeted tests; Ruff/mypy green |
| RM4.1 | Финальная приёмка и handoff | RM1.3, RM2.1, RM2.2, RM3.2 | done |  | 2212 passed, 5 skipped; post-gate delta 18 passed |

**Итого:** 9 / 9 done

## Журнал сессий

2026-10-19 · план · создана очередь RM0-RM4; реализация не начата · дальше RM0.1

2026-10-19 · RM0.1 · задача взята в работу; checkpoint заполнен · дальше baseline старого API

2026-10-19 · RM0.1 · done; `test_pool_reserve.py`: 8 passed; новый файл: 1 failed, 2 passed,
ожидаемый RED — нет `EligibleCapacity`; Store и схема не менялись · дальше RM1.1

2026-10-19 · RM1.1 · done; `EligibleCapacity` и `eligible_count()` реализованы без изменений Store;
13 targeted + 9 public API passed, Ruff/mypy green · дальше RM1.2

2026-10-19 · RM1.2 · done; общий callable-helper для capacity/lease, паритет UUID/callable/Select,
quota/operation/state/reserve; 39 conformance + 1 PG Select passed, Ruff/mypy green · дальше RM1.3

2026-10-19 · RM1.3 · задача взята в работу; аудитория — интегратор governor · дальше API reference

2026-10-19 · RM1.3 · done; API reference, SQL `among`, `allocatable`, saturated `limit` и
traceability-тест добавлены; 10 public API tests passed · дальше RM2.1

2026-10-19 · RM2.1 · done; пять причин отказа, defer и источники восстановления описаны;
`retry_after`/`retry_at` разведены; 11 public API tests passed · дальше RM2.2

2026-10-19 · RM2.2 · done; проверка выявила и исправила реальную инвариантность typing через
`Service[Any, Any]`; mypy green, 14 public/examples tests passed · дальше RM3.1

2026-10-19 · RM3.1 · done; `pool.capacity.ok` INFO на low/empty → ok с полным payload и без
дублей; 21 catalog/outbox/dispatcher tests passed · дальше RM3.2

2026-10-19 · RM3.2 · done; release запускает безопасную проверку, event → kick и обязательный
tick fallback описаны; 35 targeted tests, Ruff/mypy green · дальше RM4.1

2026-10-19 · RM4.1 · done; changelog и handoff обновлены; симуляция `eligible/allocatable`
стабильна при движении аренд; Ruff, mypy (166 файлов), import-linter (3/3), sharded pytest:
2212 passed, 5 skipped; post-gate проверки limit/event payload: 18 passed · очередь завершена
