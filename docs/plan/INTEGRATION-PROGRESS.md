# Прогресс доработок по итогам встраивания

> Единственный источник правды о статусе очереди `N` (задачи 1–7 из `ISSUES-INTEGRATION.md`).
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`INTEGRATION-PLAN.md`](INTEGRATION-PLAN.md).
> Требования: [`ISSUES-INTEGRATION.md`](ISSUES-INTEGRATION.md). Протокол: [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint для продолжения после обрыва. -->

- **Задача:** N1.1 Назначение аренды
- **Начата:** 2026-10-06
- **Шаги:**
  - [ ] Падающие conformance-тесты: purpose и ref в аренде, истории, событиях, read-схеме; проверка формата
  - [ ] Миграция: колонки purpose и ref в ih_leases и ih_lease_history; модели и SCHEMA_HEAD
  - [ ] Store: acquire_lease, LeaseRow, LeaseHistoryRow, LeaseReadRow в PgStore и MemoryStore
  - [ ] API: pools.define(purpose), pool.lease и acquire (purpose, ref), события аренды
  - [ ] Read-схема и lease_dashboard(group_by=purpose), версия read-схемы, админка
  - [ ] Документация, CHANGELOG, check.sh
- **Заметки для следующей сессии:** —

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| N0.1 | Контракт продления и причина отказа | — | done |  |  |
| N1.1 | Назначение аренды | N0.1 | in_progress |  | задача 6 |
| N1.2 | Потолок удержания аренды | N0.1 | todo |  | задача 4 |
| N1.3 | Учёт квот при выборе аккаунта | N0.1 | todo |  | задача 5 |
| N2.1 | `renew` для ручной аренды | N1.2 | todo |  | задача 7 |
| N2.2 | Команды состояния и публичное наблюдение | N0.1 | todo |  | задача 7 |
| N2.3 | Массовые изменения | N0.1 | todo |  | задача 7 |
| N3.1 | Резерв ёмкости пула | N0.1 | todo |  | задача 2 |
| N3.2a | Приоритет аренды и запрос уступить в хранилище | N1.2, N3.1 | todo |  | задача 1 |
| N3.2b | Вытеснение в API держателя | N3.2a, N2.1 | todo |  | задача 1 |
| N4.1 | Троттлинг частоты запросов | N0.1 | todo |  | задача 3 |
| N4.2 | Троттлинг: сбой держателя, `SHARED` и руководство | N4.1 | todo |  | задача 3 |
| N5.1 | Финальная сверка | все задачи N0–N4 | todo |  |  |

**Итого:** 1 / 13 done

## Журнал сессий

<!-- Добавлять строку после завершения задачи или остановки. Формат:
YYYY-MM-DD · ID · результат (done / partial / blocked) · что сделано · что дальше -->
2026-10-06 · N0.1 · done · LeaseRenewal(expires_at, end_reason) из extend_lease/renew_leases в PgStore и MemoryStore; держатель получает LeaseExpired при истечении; LeaseUnavailable.reason (busy/no_candidates/no_proxy); тест трассировки очереди N; 1637 passed, тесты аренды x3 · дальше N1.1
