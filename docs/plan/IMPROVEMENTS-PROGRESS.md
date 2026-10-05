# Прогресс имплементации улучшений Identity Hub

> Единственный источник правды о статусе очереди `IMP-01`–`IMP-21`, `IMP-23` и `PERF-1`.
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`IMPROVEMENTS-PLAN.md`](IMPROVEMENTS-PLAN.md).
> Требования: [`IMPROVEMENTS.md`](IMPROVEMENTS.md). Протокол: [`CLAUDE.md`](../../CLAUDE.md).

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
| I0.1 | Scope и воспроизводимый baseline | — | done |  |  |
| I8.1 | API: каркас неймспейсов, allowlist, таблица миграции | I0.1 | done |  |  |
| I8.2 | API: space.identities — чтение | I8.1 | done |  |  |
| I8.3 | API: space.identities — create/ensure/create_many | I8.2 | done |  |  |
| I8.4 | API: space.identities — изменение и состояние | I8.2 | done |  |  |
| I8.5 | API: space.imports и space.secrets | I8.3 | done |  |  |
| I8.6 | API: proxies (group), pools, типизированная аренда | I8.1 | done |  |  |
| I8.7 | API: auth, sync, links, data, verifications | I8.1 | done |  | параллельная ветка i8.7-namespaces |
| I8.8a | API: неймспейсы admin, analytics, maintenance | I8.3, I8.4, I8.5, I8.6, I8.7 | done |  |  |
| I8.8b | API: перевод admin/, тестов и примеров, запрет DeprecationWarning | I8.8a | done |  |  |
| I8.9 | API: руководство и примеры на новом API | I8.8b | done |  |  |
| I0.2a | Модель ошибок и маппинг HTTP/CLI | I0.1 | done |  |  |
| I0.2b | Порядок блокировок и архитектурные границы | I0.2a | done |  |  |
| I0.3 | Payload limits, versioned envelope, SecretValue | I0.2b | done |  |  |
| I0.4 | Schema check и migration advisory lock | I0.1, I0.2b | done |  |  |
| I1.1 | Нормализация external_ref | I0.3, I0.4 | done |  |  |
| I1.2a | Revision агрегата: колонка, рост, события, read-схема | I0.2b, I0.4 | done |  | разбиение D-205 |
| I1.2b | expected_revision в API изменений | I1.2a | done |  | разбиение D-205 |
| I1.3 | Идемпотентность и canonical fingerprint | I0.2b, I0.4 | done |  | закрыта F1.1+F1.2 (D-151) |
| I1.4 | HMAC-equivalence tag и fingerprint keyring | I0.3, I1.3 | todo |  |  |
| I2.1 | Атомарная LRU-выборка аренды | I0.4 | todo |  |  |
| I2.2 | HOT и пакетный heartbeat | I2.1 | todo |  |  |
| I2.3 | Полный lease fence | I0.2b, I2.1 | todo |  |  |
| I2.4 | Семантика renew, release, revoke | I2.3 | todo |  |  |
| I3.1 | Схема и lifecycle нескольких credentials | I0.3, I0.4, I1.2b | todo |  |  |
| I3.2 | API нескольких credentials и интеграция | I3.1 | todo |  |  |
| I3.3 | Два режима обновления session | I3.2 | todo |  |  |
| I4.1 | Provider operation state machine и claim | I0.2b, I0.4, I1.2b, I2.3 | todo |  |  |
| I4.2 | Login, refresh и challenge через provider operations | I3.3, I4.1 | todo |  |  |
| I4.3 | Probes, reconcile и recovery worker | I2.4, I4.1 | todo |  |  |
| I5.1 | Публичная read-схема SQLAlchemy Core | I0.4 | done |  |  |
| I5.2 | Единый hub.workers.*.run_once(limit) | I4.3 | todo |  |  |
| I5.3 | Опциональный ordered outbox | I1.2b, I5.2 | todo |  |  |
| I6.1 | Fast path операции | I1.3, I2.2, I2.4 | todo |  |  |
| I6.2 | План поиска под общей нагрузкой | I1.1, I5.1 | todo |  |  |
| I6.3 | Set-based import fast path | I1.4, I3.2 | todo |  |  |
| I6.4 | Полный load harness и бюджеты | I5.3, I6.1, I6.2, I6.3 | todo |  |  |
| I6.5 | Полная deterministic race matrix | I1.4, I2.4, I3.3, I4.3, I5.3 | todo |  |  |
| I8.10 | API: удаление legacy-алиасов | I8.9 | done |  | закрыта F6.4 (D-151) |
| I7.1 | Migration guide и документация контрактов | I5.1, I5.2, I5.3, I6.4, I6.5, I8.10 | todo |  |  |
| I7.2 | Финальный аудит и release candidate | I7.1 | todo |  |  |

**Итого:** 21 / 41 done

## Журнал сессий

<!-- Добавлять строку после завершения задачи или остановки. Формат:
YYYY-MM-DD · ID · результат (done / partial / blocked) · что сделано · что дальше -->
2026-09-30 · I0.1 · done · scope D-142, матрица IMP и baseline (lease p95 4801 мс, operation 24.9, search 163.5, import 103.7 с) · дальше I8.1
2026-09-30 · I8.1 · done · каркас identity_hub/api, allowlist SpaceHandle, карта LEGACY (85 имён) и docs/api-migration.md · дальше I8.2
2026-09-30 · I8.2 · done · space.identities: get/find/get_by_ref/card/search/list/count, DTO Identity/IdentityFilter/Page, алиасы через legacy() · дальше I8.3
2026-09-30 · I8.3 · done · identities.create/ensure/create_many (атомарно, fail|skip, порядок вставки), register — алиас, D-143 · дальше I8.4
2026-09-30 · I8.4 · done · identities.update/replace_*/теги/delete и activate/retire/set_lifecycle/restore_standing/override/unpin; 13 алиасов; дефлейк test_digest (окно 300 с) · дальше I8.5
2026-09-30 · I8.5 · done · space.imports (run/run_files/stage/get/cancel) и space.secrets (describe/reveal SecretRef/replace_*), 9 алиасов, D-144 · дальше I8.6
2026-09-30 · I8.6 · done · space.proxies (group, create/ensure/create_many/list/url/health/recheck*), space.pools (define/revoke), Pool.lease/acquire с явной сигнатурой, Pool.capacity, ProxyPolicy.group, D-145 · I8.7 ведёт параллельная сессия, дальше I0.2
2026-09-30 · I0.2a · done · стабильные коды и context ошибок, 8 новых типов, http_status/exit_code, CLI error[code] · дальше слияние I8.7, затем I0.2b
2026-09-30 · I8.7 · done · space.auth (LoginOutcome), sync.refresh/refresh_many, links, data, verifications + requirements; 15 алиасов; conformance memory+pg и онбординг на новых именах (параллельная ветка) · дальше I8.8
2026-10-01 · I0.2b · done · исправлен deadlock импорта update_session (lock_identities по UUID), docs/lock-order.md, import-linter (3 контракта) в check.sh · дальше I0.3
2026-10-01 · I8.8a · done · space.admin (22 read-модели и аудит), space.analytics (7), space.maintenance (SpaceMaintenance, 3); 32 алиаса, внутренние вызовы переведены; разбиение D-147 · дальше I8.8b
2026-10-01 · I0.3 · done · AAD identity-hub:v1 с чтением legacy и переводом в rotate-key, лимиты JSON/секрета до транзакции, SecretValue, D-148 · дальше I0.4
2026-10-01 · I0.4 · done · hub.start сверяет ревизию схемы (SchemaIncompatible), migrate под pg_advisory_xact_lock на схему, документация CLI/интеграции · дальше I1.1 после слияния I8.8b
2026-10-01 · I8.8b · done · admin/, examples, identity_hub.testing и все тесты на новых неймспейсах; фильтр pytest «устарел и будет удалён» как ошибка (библиотека и admin/); помощники tests/fixtures/api.py и admin/tests/factories/api.py; 680 + 89 тестов без предупреждений · дальше I8.9
2026-10-01 · I8.9 · done · карта API (точка входа, неймспейсы, типовые операции по сущностям, состояния, лимиты), docs/guide/README/integrations/api-migration на новых именах, тест test_documentation_uses_current_api; фрагменты карты API выполнены на MemoryStore · дальше I8.10 после I1–I6
2026-10-01 · I5.1 · done · hub.read: ReadSchema (6 таблиц, белый список колонок, версия 1, явная db_schema), join с таблицей приложения и among= в default/custom схеме, SQL без секретных полей, D-150 (параллельная ветка) · дальше I5.2 после I4.3
2026-10-01 · I1.1 · done · normalized_ref (миграция 0008), Service.normalize_ref/version, нормализация во всех путях, doctor.renormalize, D-149; tests/load переведены на новый API · дальше I1.2
2026-10-05 · I1.2a · done · revision агрегата (миграция 0017, backfill из event_seq), рост раз на команду по EventSpec.aggregate, revision в событиях/вебхуке/журнале, read-схема v2 (R2-D23), RevisionConflict, doctor; тестовая БД без fsync · дальше I1.2b
2026-10-05 · I1.2b · done · expected_revision у ~20 команд space.* (проверка FOR UPDATE первой в транзакции команды, savepoint при using), гонка двух сессий, D-206; IMP-07 implemented; дефлейк test_override_verification (срок фактора 2 с) · дальше I1.4
