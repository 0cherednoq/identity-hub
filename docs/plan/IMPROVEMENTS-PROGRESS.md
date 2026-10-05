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
| I1.4a | Кольцо ключей отпечатков и HMAC-отпечаток прокси | I0.3, I1.3 | done |  | разбиение D-207 |
| I1.4b | HMAC-тег секретов в идемпотентных командах | I1.4a | done |  | разбиение D-207 |
| I2.1 | Атомарная LRU-выборка аренды | I0.4 | done |  |  |
| I2.2a | HOT heartbeat: индексы, fillfactor, редкая запись истории | I2.1 | done |  | разбиение D-210 |
| I2.2b | Пакетный heartbeat и координатор процесса | I2.2a | done |  | разбиение D-210 |
| I2.3 | Полный lease fence | I0.2b, I2.1 | done |  |  |
| I2.4 | Семантика renew, release, revoke | I2.3 | done |  |  |
| I3.1a | Credentials: схема, виды в Service, контракт Store, версии MemoryStore | I0.3, I0.4, I1.2b | done |  | разбиение D-214 |
| I3.1b | Credentials: lifecycle, срок и команды space.secrets | I3.1a | done |  | разбиение D-214 |
| I3.2a | Credentials под арендой, в контекстах входа и раскрытие по виду | I3.1b | done |  | разбиение D-216 |
| I3.2b | Credentials в регистрации, импорте, карточке и фабриках | I3.2a | done |  | разбиение D-216 |
| I3.3a | Session: схема, lifecycle и режимы в Store | I3.2b | done |  | разбиение D-218 |
| I3.3b | Session: режимы в API, события, вход и обновление | I3.3a | done |  | разбиение D-218 |
| I4.1 | Provider operation state machine и claim | I0.2b, I0.4, I1.2b, I2.3 | todo |  |  |
| I4.2 | Login, refresh и challenge через provider operations | I3.3b, I4.1 | todo |  |  |
| I4.3 | Probes, reconcile и recovery worker | I2.4, I4.1 | todo |  |  |
| I5.1 | Публичная read-схема SQLAlchemy Core | I0.4 | done |  |  |
| I5.2 | Единый hub.workers.*.run_once(limit) | I4.3 | todo |  |  |
| I5.3 | Опциональный ordered outbox | I1.2b, I5.2 | todo |  |  |
| I6.1 | Fast path операции | I1.3, I2.2b, I2.4 | todo |  |  |
| I6.2 | План поиска под общей нагрузкой | I1.1, I5.1 | todo |  |  |
| I6.3 | Set-based import fast path | I1.4b, I3.2b | todo |  |  |
| I6.4 | Полный load harness и бюджеты | I5.3, I6.1, I6.2, I6.3 | todo |  |  |
| I6.5 | Полная deterministic race matrix | I1.4b, I2.4, I3.3b, I4.3, I5.3 | todo |  |  |
| I8.10 | API: удаление legacy-алиасов | I8.9 | done |  | закрыта F6.4 (D-151) |
| I7.1 | Migration guide и документация контрактов | I5.1, I5.2, I5.3, I6.4, I6.5, I8.10 | todo |  |  |
| I7.2 | Финальный аудит и release candidate | I7.1 | todo |  |  |

**Итого:** 34 / 46 done

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
2026-10-05 · I1.4a · done · кольцо ключей отпечатков (явное или HKDF из KEK, не равно KEK), HMAC-отпечаток прокси (миграция 0018, fingerprint_key_id), поиск по всем ключам и legacy SHA-256, doctor rekey-fingerprints, R2-S16 закрыта, D-207 · дальше I1.4b
2026-10-05 · I1.4b · done · keyed_fingerprint (секрет HMAC-тегом), fingerprint_key_id в ключах идемпотентности (миграция 0019), повтор после ротации прежним ключом, нет ключа — SecretConfigurationError; idempotency_key у secrets.replace_*; hub.equivalence_tag; админка: отпечаток чувствительных команд по раскрытому секрету (был по маске SecretStr); IMP-04 implemented, D-208 · дальше I2.1
2026-10-05 · I2.1 · done · last_leased_at (миграция 0020, backfill из истории) и индекс выборки по LRU; живая аренда через NOT EXISTS (на 50% из 100k: 192 → 0,16 мс на выборку); shared тоже ставит время; MemoryStore LRU с порядком захватов; load-замер 0/50/90/99% p95 33/22/29/18 мс на занятой машине (до: 73/253/267/187), D-209 · дальше I2.2
2026-10-05 · I2.2a · done · миграция 0021 (без индексов expires_at и истории по last_heartbeat_at, fillfactor 70), LeaseTiming.stats_flush 5 мин: история и счётчики раз в интервал и при release, heartbeat в списке живых аренд из ih_leases; HOT 0/300 → ≥0.9, D-210 · дальше I2.2b
2026-10-05 · I2.2b · done · Store.renew_leases (UPDATE по token = ANY, до 500), HeartbeatBatcher хаба: одновременные heartbeat одной пачкой, отзыв — только своей аренде, ошибка — каждой по её backoff; тесты сбоев heartbeat переведены на оба пути; замер 272 → 21 313 (100) → 57 841 (500) продлений/с; IMP-02 implemented, D-211 · дальше I2.3
2026-10-05 · I2.3 · done · LeaseFence (lease.fence), StoreTx.check_fence: lease_id+token+равенство fencing+живая аренда под FOR NO KEY UPDATE; guard в operation_scope (R1-15), save_session, auth, верификации и чтении секретов; LeaseOwnershipLost наследует LeaseLost и StaleFencing; IMP-05 implemented, D-212 · дальше I2.4
2026-10-05 · I2.4 · done · renew через GREATEST, исход release_lease (released/expired/closed/revoked), release отозванной — LeaseRevoked, истёкшей — expired без ошибки, revoke будит аренду этого процесса, LeaseLost.body_completed (R1-16), гонка release/revoke на барьере; IMP-06 implemented, D-213 · фаза I2 закрыта, дальше I3.1
2026-10-05 · I3.1a · done · миграция 0022 (ih_credentials: id, kind, name, public_value, lifecycle, expires_at, revision; прежняя строка — default без перешифрования, откат только без других записей), Service.credential_kinds, Store put/get/list_credential, AAD default — прежний, метка в отчёте ротации; MemoryStore version/updated_at (R2-P08), repro parity удалены, D-214 · дальше I3.1b
2026-10-05 · I3.1b · done · жизненный цикл credentials (active↔disabled, revoked конечное), срок по времени (Credential.expired), команды space.secrets put_credential/credentials/disable/enable/revoke_credential с expected_revision и событием без секрета; Store set_credential_lifecycle; D-215 · дальше I3.2
2026-10-05 · I3.2a · done · lease.credential(kind, name) и active_credentials под полным fence (выключенная/отозванная/истёкшая — None), lease.credentials() — default (совместимость), ctx.credential в контекстах входа и проб, SecretRef.credential и раскрытие с kind/name в аудите; D-216 · дальше I3.2b
2026-10-05 · I3.2b · done · CredentialInput и credential_records у IdentityCreate, ImportRecord (в т.ч. отложенный импорт) и make_identity: проверка и шифрование до транзакции, запись вместе с аккаунтом, импорт только новому аккаунту; AccessCard.credential_records; IMP-09 implemented, D-217 · дальше I3.3
2026-10-06 · I3.3a · done · миграция 0023 (ih_sessions: id, revision, created_at, revoked_at; одна не отозванная запись на слот; статусы valid→active, invalid/expired→invalidated; индекс под FK), SessionStatus created/active/invalidated/revoked с псевдонимами VALID/INVALID, Store put_session(mode) → SessionWrite, revoke_session, session_history; D-218 · дальше I3.3b
2026-10-06 · I3.3b · done · mode in_place/replace в lease.save_session и secrets.replace_session, secrets.revoke_session, вход — replace, события с mode/session_id/replaced_id/session_revision без данных сессии, параллельная замена на барьере; IMP-10 implemented, D-219 · фаза I3 закрыта, дальше I4.1
