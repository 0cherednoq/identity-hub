# Трассировка очереди улучшений

Матрица `IMP → задача → тест` для очереди из [`docs/plan/IMPROVEMENTS.md`](plan/IMPROVEMENTS.md).
Scope зафиксирован решением `D-142`. Задачи — в
[`IMPROVEMENTS-PLAN.md`](plan/IMPROVEMENTS-PLAN.md), статусы — в
[`IMPROVEMENTS-PROGRESS.md`](plan/IMPROVEMENTS-PROGRESS.md).

Состояние: `missing` — не начато; `partial` — есть часть поведения из v1, критерии IMP не
закрыты; `implemented` — все критерии закрыты тестами; `deferred` — вне очереди.
Колонка «Тесты» до `implemented` содержит планируемые pytest node id (`planned:`); после —
только существующие node id, это проверяет `tests/test_improvements_traceability.py`.

| IMP | Улучшение | Задачи | Состояние | Тесты |
|---|---|---|---|---|
| IMP-01 | LRU-выборка аренды | I2.1 | partial | planned: `tests/pg/test_lease_selection.py::test_lru_skips_busy_without_retries`. Сейчас кандидат сортируется по последнему захвату (`nulls first`), но без `last_leased_at` в строке identity и атомарного claim |
| IMP-02 | HOT и пакетный heartbeat | I2.2 | missing | planned: `tests/pg/test_heartbeat_hot.py::test_heartbeat_is_hot_update` |
| IMP-03 | Идемпотентность с fingerprint | I1.3 | implemented | `tests/conformance/test_operation_idempotency.py::test_same_key_with_another_operation_kind_conflicts`, `tests/conformance/test_lease_idempotency.py::test_pool_of_another_service_gets_conflict_not_foreign_account`, `tests/pg/test_idempotency_race.py::test_uncommitted_winner_holds_competitor`, `tests/pg/test_idempotency_race.py::test_parallel_acquires_with_one_key_share_one_lease`, `tests/unit/test_idempotency_fingerprint.py::test_omitted_differs_from_null`. Выполнено задачами `F1.1` и `F1.2` (`D-151`, `D-153`); ключи для register/import с секретами — `I1.4b` |
| IMP-04 | HMAC-тег команд с секретами | I1.4a, I1.4b | partial | planned: `tests/conformance/test_secret_idempotency.py::test_same_key_other_secret_conflicts`. Кольцо ключей отпечатков и HMAC-отпечаток прокси (`I1.4a`, `D-207`) готовы: `tests/unit/test_fingerprint_keyring.py`, `tests/conformance/test_proxy_fingerprint.py`; теги секретов в командах — `I1.4b` |
| IMP-05 | Полный lease fence | I2.3 | partial | planned: `tests/pg/test_lease_fence.py::test_zombie_with_equal_fencing_rejected`. Сейчас fencing сравнивается номером (`<=`) |
| IMP-06 | Семантика renew/release/revoke | I2.4 | partial | planned: `tests/pg/test_lease_semantics.py::test_release_after_revoke_is_noop` |
| IMP-07 | revision и expected_revision | I1.2a, I1.2b | implemented | `tests/conformance/test_identity_revision.py::test_each_command_bumps_revision_once`, `tests/conformance/test_identity_revision.py::test_reveal_and_lease_do_not_bump_revision`, `tests/pg/test_identity_revision_migration.py::test_backfill_keeps_seen_revisions`, `tests/conformance/test_expected_revision.py::test_stale_revision_conflicts_and_current_applies`, `tests/pg/test_expected_revision_race.py::test_two_sessions_one_winner`, `tests/pg/test_expected_revision_race.py::test_conflict_in_savepoint_keeps_outer_transaction`. Решения `D-205`, `D-206` |
| IMP-08 | Нормализация external_ref | I1.1 | implemented | `tests/conformance/test_normalized_ref.py::test_phone_formats_are_one_identity`, `tests/conformance/test_normalized_ref.py::test_import_reports_duplicates_and_resolves_links_after_normalization`, `tests/conformance/test_normalized_ref.py::test_renormalize_collision_changes_nothing`, `tests/pg/test_normalized_ref_migration.py::test_migration_collision_stops_without_changes` |
| IMP-09 | Несколько credentials | I3.1, I3.2 | missing | planned: `tests/pg/test_credentials_multi.py::test_two_keys_same_kind` |
| IMP-10 | Два режима обновления session | I3.3 | missing | planned: `tests/pg/test_session_modes.py::test_replace_revokes_old` |
| IMP-11 | Долговечные внешние операции | I4.1, I4.2, I4.3 | partial | planned: `tests/pg/test_provider_operations.py::test_claim_call_apply_recovery`. Сейчас есть таблица `ih_provider_operations` (миграция 0005) для админки, без claim/recovery |
| IMP-12 | Публичная read-схема Core | I5.1 | implemented | `tests/pg/test_read_schema.py::test_join_with_application_table`, `tests/pg/test_read_schema.py::test_join_in_custom_db_schema`, `tests/pg/test_read_schema.py::test_column_allowlist_and_version`, `tests/pg/test_read_schema.py::test_sql_snapshot_has_no_secret_columns` |
| IMP-13 | Модель ошибок с кодами | I0.2a | partial | `tests/unit/test_error_codes.py::test_every_error_has_stable_code`, `tests/unit/test_error_codes.py::test_http_and_exit_mapping`. Коды, context, HTTP/CLI готовы (I0.2a); `pinned_by_override`/`stale_observation` появятся, когда наблюдения начнут отклоняться явно (I4.3) |
| IMP-14 | Порядок блокировок | I0.2b | implemented | `tests/pg/test_lock_order.py::test_reverse_order_no_deadlock`, `tests/pg/test_lock_order.py::test_lock_identities_orders_by_uuid`. Порядок описан в `docs/lock-order.md` |
| IMP-15 | Проверка схемы при старте | I0.4 | implemented | `tests/pg/test_schema_check.py::test_old_revision_rejected`, `tests/pg/test_schema_check.py::test_concurrent_migrate_is_serialized`, `tests/pg/test_schema_check.py::test_regular_work_does_not_take_migration_lock` |
| IMP-16 | Лимиты payload и версия envelope | I0.3 | implemented | `tests/unit/test_payload_limits.py::test_json_depth_limit`, `tests/unit/test_payload_limits.py::test_legacy_payload_is_readable_and_new_writes_use_v1`, `tests/pg/test_envelope_upgrade.py::test_rotate_key_upgrades_legacy_credentials` |
| IMP-17 | Load harness с инвариантами | I6.4 | partial | planned: `tests/load/test_performance.py::test_lease_p95_10k_accounts_50_workers` без xfail. Сейчас бюджеты помечаются xfail по `D-127` |
| IMP-18 | Детерминированные race-тесты | I6.5 | missing | planned: `tests/pg/test_race_matrix.py::test_race_matrix` |
| IMP-19 | Import-linter и архитектурные тесты | I0.2b | implemented | `tests/test_architecture.py::test_import_contracts`, `tests/test_public_api.py::test_all_exports_exist_are_unique_and_public`. Контракты в `pyproject.toml`, `lint-imports` в `scripts/check.sh` |
| IMP-20 | `run_once(limit)` для фоновых работ | I5.2 | partial | planned: `tests/pg/test_workers.py::test_run_once_respects_limit`. Сейчас `Dispatcher.run_once()` без `limit` и без фасада `hub.workers` |
| IMP-21 | Ordered outbox | I5.3 | missing | planned: `tests/pg/test_ordered_outbox.py::test_next_revision_waits` |
| IMP-22 | RuntimeProfile | — | deferred | post-v1, отдельный запрос владельца |
| IMP-23 | Публичное API: неймспейсы и глаголы | I8.1–I8.7, I8.8a, I8.8b, I8.9, I8.10 | partial | `tests/test_space_surface.py::test_space_surface_allowlist`, `tests/test_space_surface.py::test_migration_doc_matches_legacy_map`. Готовы каркас (I8.1), чтение (I8.2), создание (I8.3), изменение и состояние (I8.4) в `space.identities`, `space.imports` и `space.secrets` (I8.5), `space.proxies` и `space.pools` (I8.6); `tests/conformance/test_identities_create.py::test_create_many_is_atomic_on_invalid_record` |

## Baseline перед очередью (2026-09-30)

Записан задачей I0.1 на коммите `6a66feb` (после `plan: IMP-23`). Цифры `D-127` не
переиспользуются, ниже новый замер.

| Параметр | Значение |
|---|---|
| ОС | Windows 11 Pro 10.0.26200 |
| CPU / RAM | AMD Ryzen 7 3700X, 16 потоков / 128 ГБ |
| Python / SQLAlchemy / asyncpg | 3.14.3 / 2.1.1 / 0.31.0 |
| PostgreSQL | `postgres:16-alpine` через testcontainers, Docker Engine 29.2.1 |
| Пул тестового engine | `pool_size=10`, `max_overflow=40` |
| Head миграций | `0007_state_snapshots` (неизменяемы `0001`–`0007`) |

Обычный gate `bash scripts/check.sh`: ruff, format, mypy — чисто; `502 passed, 5 deselected`
за 269.7 с.

Load-прогон `uv run pytest tests/load -m load -q -rxXfE -s`: `1 passed, 4 xfailed` за 153.6 с.

| Метрика | p50 | p95 | Бюджет §10.1 | Итог |
|---|---|---|---|---|
| lease, 10k identity, 50 воркеров | 4349.9 мс | 4801.4 мс | 20 мс | xfail |
| operation | 15.5 мс | 24.9 мс | 10 мс | xfail |
| search, 100k | 95.4 мс | 163.5 мс | 100 мс | xfail |
| import 10k | — | 103.7 с всего | 60 с | xfail |
| event latency | 53.2 мс | 64.4 мс | в бюджете | passed |

Воспроизведение: `docker` доступен, `uv sync`, затем две команды выше. Замер на рабочей
машине разработчика, не изолированный стенд; сравнивать только с прогоном в тех же условиях.
