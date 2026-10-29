# Трассировка session-first контракта

Матрица связывает публичные гарантии `auth.verify_session` и `auth.adopt_session` с тестами.
Источник решения: `D-334` в [`plan/DECISIONS.md`](plan/DECISIONS.md). План и сохранённый статус:
[`session-first-plan.md`](session-first-plan.md) и
[`session-first-progress.md`](session-first-progress.md).

| ID | Гарантия | Pytest node |
|---|---|---|
| SF-R01 | `probe` разрешён для `provisioning` и `ready`, но не для `retired` | `tests/unit/test_admissibility.py::test_probe_lifecycle_policy_allows_provisioning_but_not_retired` |
| SF-R02 | `ProbeContext.session()` читает выбранный named slot, явный аргумент читает другой | `tests/unit/test_adapter_contexts.py::test_probe_context_targets_named_session_slot` |
| SF-R03 | `verify_session` проверяет named slot и меняет только его validity metadata | `tests/conformance/test_session_first_auth.py::test_verify_named_session_marks_metadata_only` |
| SF-R04 | Invalid non-default не меняет глобальный auth; invalid default переводит его в `session_invalid` | `tests/conformance/test_session_first_auth.py::test_verify_invalid_default_marks_global_auth_invalid` |
| SF-R05 | `adopt_session` одним provider call создаёт canonical default, auth и optional ready | `tests/conformance/test_session_first_auth.py::test_adopt_named_session_sets_canonical_auth_and_activates` |
| SF-R06 | Технический сбой retryable и не оставляет частичного состояния | `tests/conformance/test_session_first_auth.py::test_adopt_technical_failure_has_no_partial_state` |
| SF-R07 | Сбой финальной транзакции откатывает default, source metadata, auth и lifecycle | `tests/conformance/test_session_first_auth.py::test_adopt_final_transaction_rolls_back_as_a_unit` |
| SF-R08 | Замена source во время provider call отклоняет поздний verdict | `tests/conformance/test_session_first_auth.py::test_adopt_rejects_late_result_after_source_replacement` |
| SF-R09 | `expected_revision` повторно проверяется после provider call | `tests/conformance/test_session_first_auth.py::test_adopt_expected_revision_is_rechecked_after_provider` |
| SF-R10 | Истёкший provider claim отклоняет поздний verdict | `tests/conformance/test_session_first_auth.py::test_expired_provider_claim_rejects_late_adoption` |
| SF-R11 | Отозванная lease отклоняет поздний verdict | `tests/conformance/test_session_first_auth.py::test_revoked_lease_rejects_late_adoption` |
| SF-R12 | Конкурентные adoption оставляют default, соответствующий одному provider verdict | `tests/conformance/test_session_first_auth.py::test_concurrent_adoptions_leave_one_coherent_default` |

Все conformance nodes выполняются на `MemoryStore` и PostgreSQL. Статический публичный контракт
закреплён в `tests/typing/session_first.py`, а корневые exports и методы namespace — в
`tests/unit/test_session_first_contract.py`.
