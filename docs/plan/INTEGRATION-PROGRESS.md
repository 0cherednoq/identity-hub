# Прогресс доработок по итогам встраивания

> Единственный источник правды о статусе очереди `N` (задачи 1–7 из `ISSUES-INTEGRATION.md`).
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`INTEGRATION-PLAN.md`](INTEGRATION-PLAN.md).
> Требования: [`ISSUES-INTEGRATION.md`](ISSUES-INTEGRATION.md). Протокол: [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint для продолжения после обрыва. -->

- **Задача:** —
- **Начата:** —
- **Шаги:**
  - [ ] —
- **Заметки для следующей сессии:** Следующая — N1.3 (require_quota). Свойства аренды идут через LeaseOptions и Pool.lease_options; причина истечения — expiry_reason (store/base.py) и _expiry_reason (postgres/store.py). Миграции ветки 0024 и 0025 при слиянии с master перенумеровать: очередь I там тоже занимает 0024. Полный check.sh около 9 минут — в фоне. После uv run --project admin откатывать admin/uv.lock.

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| N0.1 | Контракт продления и причина отказа | — | done |  |  |
| N1.1 | Назначение аренды | N0.1 | done |  | задача 6 |
| N1.2 | Потолок удержания аренды | N0.1 | done |  | задача 4 |
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

**Итого:** 3 / 13 done

## Журнал сессий

<!-- Добавлять строку после завершения задачи или остановки. Формат:
YYYY-MM-DD · ID · результат (done / partial / blocked) · что сделано · что дальше -->
2026-10-06 · N0.1 · done · LeaseRenewal(expires_at, end_reason) из extend_lease/renew_leases в PgStore и MemoryStore; держатель получает LeaseExpired при истечении; LeaseUnavailable.reason (busy/no_candidates/no_proxy); тест трассировки очереди N; 1637 passed, тесты аренды x3 · дальше N1.1
2026-10-06 · N1.1 · done · purpose и ref: pools.define(purpose), pool.lease/acquire(purpose, ref), lease.purpose/ref; колонки в ih_leases и ih_lease_history (миграция 0024); события lease.*, read-схема v3, lease_dashboard(group_by=purpose); админка: openapi.json и типы фронтенда · дальше N1.2
2026-10-06 · N1.2 · done · max_hold: pools.define/lease/acquire(max_hold), lease.hold_deadline; продление least(hold_deadline) в одиночном и пакетном пути; держатель получает LeaseLost(max_hold) за safety_margin до дедлайна; причина max_hold в истории и событии lease.expired; миграция 0025, read-схема v4, админка; тесты аренды x3 · дальше N1.3
