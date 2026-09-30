# Прогресс имплементации улучшений Identity Hub

> Единственный источник правды о статусе очереди `IMP-01`–`IMP-21`, `IMP-23` и `PERF-1`.
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`IMPROVEMENTS-PLAN.md`](IMPROVEMENTS-PLAN.md).
> Требования: [`IMPROVEMENTS.md`](IMPROVEMENTS.md). Протокол: [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint для продолжения после обрыва. -->

- **Задача:** I8.6 API: proxies (group), pools, типизированная аренда
- **Начата:** 2026-09-30
- **Шаги:**
  - [ ] space.proxies с group
  - [ ] ProxyPolicy.group с алиасом pool
  - [ ] space.pools: define, revoke; Pool.capacity
  - [ ] явные сигнатуры Pool.lease/acquire
  - [ ] алиасы и тесты
  - [ ] check.sh и закрытие
- **Заметки для следующей сессии:** —

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| I0.1 | Scope и воспроизводимый baseline | — | done |  |  |
| I8.1 | API: каркас неймспейсов, allowlist, таблица миграции | I0.1 | done |  |  |
| I8.2 | API: space.identities — чтение | I8.1 | done |  |  |
| I8.3 | API: space.identities — create/ensure/create_many | I8.2 | done |  |  |
| I8.4 | API: space.identities — изменение и состояние | I8.2 | done |  |  |
| I8.5 | API: space.imports и space.secrets | I8.3 | done |  |  |
| I8.6 | API: proxies (group), pools, типизированная аренда | I8.1 | in_progress |  |  |
| I8.7 | API: auth, sync, links, data, verifications | I8.1 | todo |  |  |
| I8.8 | API: вынос admin/analytics/maintenance, перевод потребителей | I8.3, I8.4, I8.5, I8.6, I8.7 | todo |  |  |
| I8.9 | API: руководство и примеры на новом API | I8.8 | todo |  |  |
| I0.2 | Ошибки, lock order, архитектурные границы | I0.1 | todo |  |  |
| I0.3 | Payload limits, versioned envelope, SecretValue | I0.2 | todo |  |  |
| I0.4 | Schema check и migration advisory lock | I0.1, I0.2 | todo |  |  |
| I1.1 | Нормализация external_ref | I0.3, I0.4 | todo |  |  |
| I1.2 | Revision агрегата и expected_revision | I0.2, I0.4 | todo |  |  |
| I1.3 | Идемпотентность и canonical fingerprint | I0.2, I0.4 | todo |  |  |
| I1.4 | HMAC-equivalence tag и fingerprint keyring | I0.3, I1.3 | todo |  |  |
| I2.1 | Атомарная LRU-выборка аренды | I0.4 | todo |  |  |
| I2.2 | HOT и пакетный heartbeat | I2.1 | todo |  |  |
| I2.3 | Полный lease fence | I0.2, I2.1 | todo |  |  |
| I2.4 | Семантика renew, release, revoke | I2.3 | todo |  |  |
| I3.1 | Схема и lifecycle нескольких credentials | I0.3, I0.4, I1.2 | todo |  |  |
| I3.2 | API нескольких credentials и интеграция | I3.1 | todo |  |  |
| I3.3 | Два режима обновления session | I3.2 | todo |  |  |
| I4.1 | Provider operation state machine и claim | I0.2, I0.4, I1.2, I2.3 | todo |  |  |
| I4.2 | Login, refresh и challenge через provider operations | I3.3, I4.1 | todo |  |  |
| I4.3 | Probes, reconcile и recovery worker | I2.4, I4.1 | todo |  |  |
| I5.1 | Публичная read-схема SQLAlchemy Core | I0.4 | todo |  |  |
| I5.2 | Единый hub.workers.*.run_once(limit) | I4.3 | todo |  |  |
| I5.3 | Опциональный ordered outbox | I1.2, I5.2 | todo |  |  |
| I6.1 | Fast path операции | I1.3, I2.2, I2.4 | todo |  |  |
| I6.2 | План поиска под общей нагрузкой | I1.1, I5.1 | todo |  |  |
| I6.3 | Set-based import fast path | I1.4, I3.2 | todo |  |  |
| I6.4 | Полный load harness и бюджеты | I5.3, I6.1, I6.2, I6.3 | todo |  |  |
| I6.5 | Полная deterministic race matrix | I1.4, I2.4, I3.3, I4.3, I5.3 | todo |  |  |
| I8.10 | API: удаление legacy-алиасов | I8.9 | todo |  |  |
| I7.1 | Migration guide и документация контрактов | I5.1, I5.2, I5.3, I6.4, I6.5, I8.10 | todo |  |  |
| I7.2 | Финальный аудит и release candidate | I7.1 | todo |  |  |

**Итого:** 6 / 38 done

## Журнал сессий

<!-- Добавлять строку после завершения задачи или остановки. Формат:
YYYY-MM-DD · ID · результат (done / partial / blocked) · что сделано · что дальше -->
2026-09-30 · I0.1 · done · scope D-142, матрица IMP и baseline (lease p95 4801 мс, operation 24.9, search 163.5, import 103.7 с) · дальше I8.1
2026-09-30 · I8.1 · done · каркас identity_hub/api, allowlist SpaceHandle, карта LEGACY (85 имён) и docs/api-migration.md · дальше I8.2
2026-09-30 · I8.2 · done · space.identities: get/find/get_by_ref/card/search/list/count, DTO Identity/IdentityFilter/Page, алиасы через legacy() · дальше I8.3
2026-09-30 · I8.3 · done · identities.create/ensure/create_many (атомарно, fail|skip, порядок вставки), register — алиас, D-143 · дальше I8.4
2026-09-30 · I8.4 · done · identities.update/replace_*/теги/delete и activate/retire/set_lifecycle/restore_standing/override/unpin; 13 алиасов; дефлейк test_digest (окно 300 с) · дальше I8.5
2026-09-30 · I8.5 · done · space.imports (run/run_files/stage/get/cancel) и space.secrets (describe/reveal SecretRef/replace_*), 9 алиасов, D-144 · дальше I8.6
