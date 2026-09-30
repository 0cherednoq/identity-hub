# Трассировка требований Identity Hub 1.0.0rc1

Матрица предназначена для аудитора release candidate. Источник требований —
[`identity-hub-requirements.md`](../identity-hub-requirements.md); доказательство
для каждой строки — pytest node, выполняемый обычным или нагрузочным набором.
Полнота ID и существование указанных тестов проверяются автоматически в
`tests/test_traceability.py`.

Маркер `unit` не требует БД, `pg` использует PostgreSQL 16, `conformance`
выполняет общий контракт против PostgreSQL и `MemoryStore`, `load` запускается
явно. Обязательный gate `scripts/check.sh` выполняет все тесты, кроме `load`;
нагрузочные цели и известный долг PERF-1 описаны в [`perf.md`](perf.md).

## Функциональные требования

| Требование | Исполняемое доказательство |
|---|---|
| FR-1.1 | `tests/pg/test_state.py::test_unknown_identity_and_foreign_space` |
| FR-1.2 | `tests/unit/test_service.py::test_fixture_services_are_valid` |
| FR-1.3 | `tests/pg/test_hub.py::test_start_syncs_service_registry` |
| FR-2.1 | `tests/pg/test_register.py::test_register_full` |
| FR-2.2 | `tests/pg/test_register.py::test_external_ref_unique_across_spaces` |
| FR-2.3 | `tests/pg/test_register.py::test_register_is_idempotent` |
| FR-2.4 | `tests/pg/test_import.py::test_import_mixed_batch_and_repeat` |
| FR-2.5 | `tests/pg/test_import.py::test_verify_sessions_and_update_session` |
| FR-2.6 | `tests/pg/test_import.py::test_file_importer` |
| FR-2.7 | `tests/pg/test_import.py::test_async_stream_hook_and_other_space` |
| FR-3.1 | `tests/pg/test_identity_data.py::test_profile_merge_and_attributes` |
| FR-3.2 | `tests/pg/test_identity_data.py::test_encrypted_profile_update` |
| FR-3.3 | `tests/pg/test_identity_data.py::test_labels_tags_note` |
| FR-3.4 | `tests/pg/test_identity_data.py::test_labels_tags_note` |
| FR-3.5 | `tests/pg/test_identity_data.py::test_labels_tags_note` |
| FR-3.6 | `tests/pg/test_identity_data.py::test_identity_data_optimistic_lock` |
| FR-4.1 | `tests/pg/test_links.py::test_link_unlink_and_views` |
| FR-4.2 | `tests/pg/test_identity_data.py::test_delete_restricted_by_links` |
| FR-4.3 | `tests/pg/test_logins.py::test_linked_identity_leased_during_login` |
| FR-4.4 | `tests/pg/test_links.py::test_impact_transitive` |
| FR-5.1 | `tests/unit/test_crypto.py::test_model_roundtrip_reveals_secrets_only_inside_ciphertext` |
| FR-5.2 | `tests/pg/test_lease_fencing.py::test_secrets_only_via_lease` |
| FR-5.3 | `tests/pg/test_lease_fencing.py::test_zombie_after_expiry_cannot_write` |
| FR-5.4 | `tests/pg/test_logins.py::test_run_logins_success_and_failure` |
| FR-5.5 | `tests/pg/test_logins.py::test_challenge_flow` |
| FR-5.6 | `tests/pg/test_lease_fencing.py::test_mark_authenticated` |
| FR-6.1 | `tests/pg/test_pool_pick.py::test_tags_labels_profile_among` |
| FR-6.2 | `tests/pg/test_lease_lifecycle.py::test_heartbeat_extends_and_release_closes` |
| FR-6.3 | `tests/pg/test_lease_lifecycle.py::test_lost_before_ttl_when_heartbeat_fails` |
| FR-6.4 | `tests/pg/test_outbox.py::test_secrets_never_reach_journal` |
| FR-6.5 | `tests/pg/test_acquire.py::test_idempotency_key_returns_same_lease` |
| FR-6.6 | `tests/pg/test_acquire.py::test_shared_mode_does_not_block` |
| FR-6.7 | `tests/pg/test_cards.py::test_lease_history_and_stats` |
| FR-6.8 | `tests/pg/test_acquire.py::test_expired_lease_reclaimed_with_history_closed_at_expiry` |
| FR-6.9 | `tests/pg/test_lease_fencing.py::test_revoke_from_admin` |
| FR-7.1 | `tests/pg/test_operation.py::test_success_charges_before_call_and_counts` |
| FR-7.2 | `tests/pg/test_quotas.py::test_race_for_last_units` |
| FR-7.3 | `tests/pg/test_operation.py::test_session_invalid_and_ban` |
| FR-7.4 | `tests/pg/test_operation.py::test_cool_down_only_extends` |
| FR-7.5 | `tests/pg/test_operation.py::test_operation_key_duplicate` |
| FR-7.6 | `tests/test_public_api.py::test_unsupported_quota_reset_is_not_public` |
| FR-8.1 | `tests/pg/test_proxies.py::test_add_dedup_and_secret` |
| FR-8.2 | `tests/pg/test_bindings.py::test_sticky_binding_least_loaded_and_capacity` |
| FR-8.3 | `tests/pg/test_bindings.py::test_proxy_down_rebind` |
| FR-8.4 | `tests/pg/test_proxies.py::test_health_cycle` |
| FR-9.1 | `tests/unit/test_regions.py::test_observation_applies_from_any_state` |
| FR-9.2 | `tests/pg/test_state.py::test_stale_observation_dropped` |
| FR-9.3 | `tests/pg/test_state.py::test_override_pin_and_drift` |
| FR-9.4 | `tests/pg/test_state.py::test_confirm_leaving_frozen` |
| FR-9.5 | `tests/unit/test_regions.py::test_normalizer_applies` |
| FR-9.6 | `tests/pg/test_sync.py::test_unknown_status_and_pinned_drift` |
| FR-9.7 | `tests/pg/test_sync.py::test_reconcile_skips_busy_and_respects_age` |
| FR-9.8 | `tests/pg/test_state.py::test_observation_writes_region_denormalizes_and_emits` |
| FR-10.1 | `tests/unit/test_verification.py::test_spec_validation` |
| FR-10.2 | `tests/unit/test_verification.py::test_tiers_and_factor_helpers` |
| FR-10.3 | `tests/pg/test_verifications.py::test_resume_and_pending_expiry` |
| FR-10.4 | `tests/pg/test_verifications.py::test_full_kyc_cycle` |
| FR-10.5 | `tests/pg/test_verifications.py::test_full_kyc_cycle` |
| FR-10.6 | `tests/unit/test_admissibility.py::test_verification_tiers_and_removed_factors` |
| FR-11.1 | `tests/unit/test_data_query.py::test_parse_query_example_from_spec` |
| FR-11.2 | `tests/pg/test_search.py::test_search_uses_gin_indexes` |
| FR-11.3 | `tests/pg/test_search.py::test_search_uses_gin_indexes` |
| FR-11.4 | `tests/pg/test_search.py::test_keyset_pagination_and_count` |
| FR-11.5 | `tests/pg/test_cards.py::test_identity_card` |
| FR-11.6 | `tests/pg/test_cards.py::test_pool_summary` |
| FR-11.7 | `tests/pg/test_cards.py::test_lease_history_and_stats` |
| FR-11.8 | `tests/pg/test_cards.py::test_reindex_search` |
| FR-12.1 | `tests/unit/test_logging_context.py::test_logging_filter_adds_fields` |
| FR-12.2 | `tests/pg/test_lease_logs.py::test_sink_failure_does_not_break_lease` |
| FR-13.1 | `tests/pg/test_outbox.py::test_event_visible_only_after_commit` |
| FR-13.2 | `tests/unit/test_events_catalog.py::test_event_to_dict` |
| FR-13.3 | `tests/unit/test_events_catalog.py::test_catalog_covers_spec_and_region_events` |
| FR-13.4 | `tests/pg/test_outbox.py::test_journal_only_events_and_severity` |
| FR-13.5 | `tests/pg/test_dispatcher.py::test_callbacks_filtered_and_at_least_once` |
| FR-13.6 | `tests/pg/test_dispatcher.py::test_listen_wakes_dispatcher` |
| FR-14.1 | `tests/pg/test_webhooks.py::test_filtered_delivery_is_signed_and_secret_is_encrypted` |
| FR-14.2 | `tests/unit/test_webhooks.py::test_signature_supports_rotation_and_replay_window` |
| FR-14.3 | `tests/pg/test_webhooks.py::test_5xx_retries_disable_endpoint_and_manual_retry_recovers` |
| FR-14.4 | `tests/pg/test_webhooks.py::test_5xx_retries_disable_endpoint_and_manual_retry_recovers` |
| FR-14.5 | `tests/pg/test_webhooks.py::test_redirect_is_recorded_without_following` |
| FR-15.1 | `tests/unit/test_telegram_transport.py::test_sends_message_with_thread` |
| FR-15.2 | `tests/pg/test_notifications.py::test_immediate_notification_filters_data_and_encrypts_token` |
| FR-15.3 | `tests/pg/test_notifications.py::test_immediate_notification_filters_data_and_encrypts_token` |
| FR-15.4 | `tests/pg/test_notifications.py::test_digest_accumulates_one_open_notification` |
| FR-15.5 | `tests/pg/test_notifications.py::test_immediate_notification_filters_data_and_encrypts_token` |
| FR-15.6 | `tests/pg/test_notifications.py::test_telegram_429_uses_retry_after`, `tests/pg/test_notifications.py::test_telegram_rejection_is_dead_without_retries` |
| FR-15.7 | `tests/pg/test_notifications.py::test_immediate_notification_filters_data_and_encrypts_token` |

## Гарантии

| Требование | Исполняемое доказательство |
|---|---|
| G-1.1 | `tests/pg/test_acquire.py::test_race_many_acquirers_are_exclusive` |
| G-1.2 | `tests/pg/test_lease_lifecycle.py::test_lost_before_ttl_when_heartbeat_fails` |
| G-1.3 | `tests/pg/test_acquire.py::test_expired_lease_reclaimed_with_history_closed_at_expiry` |
| G-1.4 | `tests/pg/test_lease_fencing.py::test_zombie_after_expiry_cannot_write` |
| G-1.5 | `tests/pg/test_pool_pick.py::test_skip_locked` |
| G-2.1 | `tests/pg/test_operation.py::test_quota_not_rolled_back_by_user_exception` |
| G-2.2 | `tests/pg/test_quotas.py::test_exhausted_quota_does_not_consume_key` |
| G-2.3 | `tests/pg/test_operation.py::test_quota_not_rolled_back_by_user_exception` |
| G-2.4 | `tests/unit/test_service.py::test_quota_windows_follow_date_bin` |
| G-4.1 | `tests/pg/test_state.py::test_observation_writes_region_denormalizes_and_emits` |
| G-4.2 | `tests/pg/test_acquire.py::test_acquire_writes_lease_history_and_event` |
| G-4.3 | `tests/pg/test_register.py::test_register_in_user_transaction` |
| G-4.4 | `tests/pg/test_import.py::test_import_mixed_batch_and_repeat` |
| G-5.1 | `tests/pg/test_outbox.py::test_rollback_drops_events` |
| G-5.2 | `tests/pg/test_dispatcher.py::test_late_commit_of_earlier_transaction_not_skipped` |
| G-5.3 | `tests/pg/test_dispatcher.py::test_callbacks_filtered_and_at_least_once` |
| G-5.4 | `tests/pg/test_outbox.py::test_sequence_per_identity` |
| G-5.5 | `tests/pg/test_dispatcher.py::test_two_dispatchers_do_not_duplicate` |
| G-6.1 | `tests/pg/test_state.py::test_auth_default_slot_denormalized_other_slots_not` |
| G-6.2 | `tests/pg/test_doctor.py::test_doctor_finds_and_fixes_denormalization` |

## Транзакционные правила

| Требование | Исполняемое доказательство |
|---|---|
| T-1 | `tests/pg/test_hub.py::test_user_session_rollback_undoes_library_write` |
| T-2 | `tests/pg/test_lease_lifecycle.py::test_heartbeat_does_not_use_user_session` |
| T-3 | `tests/pg/test_hub.py::test_session_bound_to_one_space` |
| T-4 | `tests/pg/test_state.py::test_concurrent_observations_serialize` |
| T-5 | `tests/pg/test_operation.py::test_success_charges_before_call_and_counts` |

## Безопасность

| Требование | Исполняемое доказательство |
|---|---|
| S-1 | `tests/unit/test_crypto.py::test_roundtrip_bytes_and_unique_dek` |
| S-2 | `tests/unit/test_crypto.py::test_blob_cannot_move_to_another_row` |
| S-3 | `tests/pg/test_doctor.py::test_rotate_key_rewraps_every_encrypted_table` |
| S-4 | `tests/unit/test_crypto.py::test_parse_keys_and_providers` |
| S-5 | `tests/unit/test_crypto.py::test_model_roundtrip_reveals_secrets_only_inside_ciphertext` |
| S-6 | `tests/pg/test_doctor.py::test_rotate_key_rewraps_every_encrypted_table` |
| S-7 | `tests/pg/test_secrets_admin.py::test_update_and_reveal_with_audit` |
| S-8 | `tests/pg/test_outbox.py::test_secrets_never_reach_journal` |
| S-9 | `tests/unit/test_webhooks.py::test_signature_supports_rotation_and_replay_window` |

## Release-candidate проверки

Сценарий §11, корневой `__all__` и версия проверяются в
`tests/test_public_api.py`. Исполняемый пользовательский поток без внешних
сервисов проверяется в `tests/unit/test_examples.py`. Полный обычный gate:

```bash
bash scripts/check.sh
```

Нагрузочный набор запускается отдельно:

```bash
uv run pytest -m load tests/load/test_performance.py
```
