# Трассировка контракта перепроверки аккаунта

Матрица связывает гарантии очереди `AR` (`auth.revalidate`, вердикт провайдера как наблюдение,
диагностический допуск, идемпотентность по `request_id`) с тестами. Источник решения: `D-335` в
[`plan/DECISIONS.md`](plan/DECISIONS.md). План и статус: [`plan/AUTH-REVALIDATION-PLAN.md`](plan/AUTH-REVALIDATION-PLAN.md)
и [`plan/AUTH-REVALIDATION-PROGRESS.md`](plan/AUTH-REVALIDATION-PROGRESS.md). Обоснование и эксперименты:
[`isue/auth-revalidation-review.md`](isue/auth-revalidation-review.md).

| ID | Гарантия | Pytest node |
|---|---|---|
| AR-R01 | Вердикт `session_invalid` из `verify_session`/`adopt_session` терминален из любого состояния `auth` | `tests/conformance/test_auth_verdict_observation.py::test_session_invalid_verdict_is_terminal_from_every_auth_state` |
| AR-R02 | Проба не затирает `challenge` и `logging_in` (исход `sticky`) | `tests/conformance/test_auth_verdict_observation.py::test_probe_does_not_overwrite_interactive_login` |
| AR-R03 | Граф `auth` не содержит перехода под внешний факт | `tests/unit/test_regions.py::test_graph_has_no_edge_for_external_fact_no_session_to_session_invalid` |
| AR-R04 | Вердикт `session_invalid` сохраняет `retryable=False` | `tests/conformance/test_auth_verdict_observation.py::test_session_invalid_verdict_keeps_retryable_false` |
| AR-R05 | Отказ проекции пробы терминализует операцию как `projection_failed` | `tests/conformance/test_projection_failed.py::test_probe_projection_failure_terminalizes_operation` |
| AR-R06 | Отказ проекции входа терминализует операцию и возвращает `auth` | `tests/conformance/test_projection_failed.py::test_login_projection_failure_terminalizes_operation_and_restores_auth` |
| AR-R07 | Восстановленная операция помечена неизвестным исходом, повтор не блокируется | `tests/conformance/test_operation_recovery.py::test_probe_recovered_after_claim_expiry_is_marked_unknown_and_retry_is_not_blocked` |
| AR-R08 | Политика диагностики по умолчанию: `retired`, пауза, охлаждение допущены; карантин и `deleted` нет | `tests/unit/test_diagnostics.py::test_default_policy_allows_retired_paused_cooling_but_not_quarantined_or_deleted` |
| AR-R09 | Диагностический допуск пропускает заблокированные для нагрузки состояния и ничего не меняет | `tests/conformance/test_auth_diagnostic_admission.py::test_default_policy_admits_diagnostics` |
| AR-R10 | Отказ допуска — типизированный `Blocker` | `tests/conformance/test_auth_diagnostic_admission.py::test_quarantined_and_deleted_are_blocked_by_default_with_typed_blocker` |
| AR-R11 | Обычные `sync.refresh`/`refresh_many` допуск не расширяют | `tests/conformance/test_auth_diagnostic_admission.py::test_ordinary_sync_paths_are_unchanged` |
| AR-R12 | `revalidate` из `banned`/`retired`/паузы/охлаждения не меняет состояние аккаунта | `tests/conformance/test_auth_revalidate_session.py::test_blocked_states_are_diagnosed_without_changing_them` |
| AR-R13 | Технический сбой пробы сохраняет последнее достоверное состояние | `tests/conformance/test_auth_revalidate_session.py::test_technical_failure_keeps_last_reliable_state` |
| AR-R14 | Нет сессии — резервная проверка credential без `logging_in`, успех пишет сессию | `tests/conformance/test_auth_revalidate_credential.py::test_missing_session_falls_back_to_credential_and_authenticates` |
| AR-R15 | `disabled`/`revoked`/истёкшая/отсутствующая запись — `blocked` без вызова провайдера | `tests/conformance/test_auth_revalidate_credential.py::test_unusable_credentials_block_without_provider_call` |
| AR-R16 | Технический сбой входа не переводит `auth` в `no_session` и не теряет `session_invalid` | `tests/conformance/test_auth_revalidate_credential.py::test_technical_failure_keeps_session_invalid` |
| AR-R17 | Активный `challenge` возвращается без вызова провайдера | `tests/conformance/test_auth_revalidate_credential.py::test_active_challenge_is_returned_without_provider_call` |
| AR-R18 | Повтор завершённого `request_id` возвращает сохранённый результат без вызова провайдера | `tests/conformance/test_auth_revalidate_idempotency.py::test_completed_request_is_replayed_without_provider_call` |
| AR-R19 | Параллельные запросы делают не более одного вызова провайдера | `tests/conformance/test_auth_revalidate_idempotency.py::test_parallel_requests_make_one_provider_call` |
| AR-R20 | Тот же `request_id` с другим `credential` — `IdempotencyConflict` | `tests/conformance/test_auth_revalidate_idempotency.py::test_same_request_id_with_other_credential_conflicts` |
| AR-R21 | Новый `request_id` — новая попытка | `tests/conformance/test_auth_revalidate_idempotency.py::test_new_request_id_is_a_new_attempt` |
| AR-R22 | После `claim_expired` повтор того же `request_id` продолжает попытку | `tests/conformance/test_auth_revalidate_idempotency.py::test_request_continues_after_claim_expired` |
| AR-R23 | Ключ запроса операции блокирует повтор, пока она жива или успешна | `tests/conformance/test_operation_request_key.py::test_live_and_succeeded_keyed_operation_blocks_repeat` |

Все conformance nodes выполняются на `MemoryStore` и PostgreSQL. Статический публичный контракт закреплён
в `tests/typing/diagnostics.py`, корневые exports — в `tests/test_public_api.py`.
