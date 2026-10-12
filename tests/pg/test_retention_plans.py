"""F4.1b: чистка идёт пачками в отдельных транзакциях и по индексам (R2-D10)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from identity_hub import IdentityHub, SpaceHandle

pytestmark = pytest.mark.pg

KEEP = timedelta(days=3650)
RETENTION = dict.fromkeys(
    (
        "events",
        "lease_history",
        "lease_logs",
        "deliveries",
        "operation_keys",
        "import_runs",
        "provider_operations",
        "metric_batches",
        "closed_challenges",
        "rollup_hours",
        "rollup_days",
    ),
    KEEP,
)
CATEGORIES = frozenset(
    {
        "quota_windows",
        "operation_keys",
        "lease_logs",
        "lease_history",
        "webhook_deliveries",
        "notifications",
        "events",
        "import_runs",
        "provider_operations",
        "metric_batches",
        "closed_challenges",
        "rollup_hours",
        "rollup_days",
    }
)
N = 5000
# свежие строки: чистке удалять нечего, но таблицы достаточно велики, чтобы Seq Scan был заметен
SEED = [
    f"""INSERT INTO ih_identities (id, space_id, service, external_ref, normalized_ref, lifecycle, standing,
            auth_state, availability, error_score, fencing_seq, event_seq)
        SELECT gen_random_uuid(), :space, 'fake_tg', 'ref-' || g, 'ref-' || g, 'ready', 'good',
            'authenticated', 'available', 0, 0, 0 FROM generate_series(1, {N}) g""",
    # окно следующего часа: окно текущего истекает, если прогон пересекает границу часа
    """INSERT INTO ih_quota_usage (identity_id, kind, window_seconds, window_start, space_id, used)
        SELECT id, 'op', w, date_trunc('hour', now()) + interval '1 hour', space_id, 1
        FROM ih_identities, (VALUES (3600), (86400)) v(w)""",
    f"""INSERT INTO ih_operation_keys (namespace, key, space_id, operation, fingerprint_version, attempt,
            expires_at)
        SELECT 'ns', 'k' || g, :space, 'op', 1, 1, now() + interval '1 day' FROM generate_series(1, {N}) g""",
    f"""INSERT INTO ih_lease_logs (lease_id, identity_id, space_id, ts, level, message)
        SELECT gen_random_uuid(), gen_random_uuid(), :space, now(), 20, 'm' FROM generate_series(1, {N}) g""",
    f"""INSERT INTO ih_lease_history (lease_id, space_id, identity_id, token, fencing, pool, holder,
            succeeded, failed, acquired_at, released_at, end_reason)
        SELECT gen_random_uuid(), :space, gen_random_uuid(), gen_random_uuid(), 1, 'p', 'h', 0, 0,
            now() - interval '1 minute', now(), 'released' FROM generate_series(1, {N}) g""",
    f"""INSERT INTO ih_events (event_id, type, severity, space_id, actor, external)
        SELECT gen_random_uuid(), 'identity.updated', 'info', :space, 'a', true
        FROM generate_series(1, {N}) g""",
    """INSERT INTO ih_webhook_deliveries (endpoint_id, event_id, status, attempt)
        SELECT (SELECT id FROM ih_webhook_endpoints), event_id, 'succeeded', 1 FROM ih_events""",
    """INSERT INTO ih_notification_channels (id, space_id, kind, title, config, enabled, key_id, wrapped_dek,
            nonce, ciphertext)
        VALUES (gen_random_uuid(), :space, 'telegram', 't', '{}', true, 'k1', '', '', '')""",
    """INSERT INTO ih_notification_rules (id, space_id, channel_id, name, enabled, event_types, min_severity,
            created_by)
        SELECT gen_random_uuid(), :space, id, 'r', true, '{*}', 'info', 'a' FROM ih_notification_channels""",
    """INSERT INTO ih_notifications (rule_id, channel_id, event_ids, status, attempt, scheduled_at, sent_at)
        SELECT r.id, r.channel_id, ARRAY[e.event_id], 'succeeded', 1, now(), now()
        FROM ih_notification_rules r, ih_events e""",
    f"""INSERT INTO ih_import_runs (id, space_id, service, actor, source, state, finished_at)
        SELECT gen_random_uuid(), :space, 'fake_tg', 'a', 's', 'succeeded', now()
        FROM generate_series(1, {N}) g""",
    """INSERT INTO ih_provider_operations (id, space_id, identity_id, kind, state, finished_at)
        SELECT gen_random_uuid(), space_id, id, 'probe:auth', 'succeeded', now() FROM ih_identities""",
    f"""INSERT INTO ih_stats_metric_batches (id) SELECT gen_random_uuid() FROM generate_series(1, {N}) g""",
    """INSERT INTO ih_challenges (id, identity_id, space_id, kind, hint, context_key_id, context_wrapped_dek,
            context_nonce, context_ciphertext, resolved_at)
        SELECT gen_random_uuid(), id, space_id, 'sms', 'h', 'k1', '', '', '', now() FROM ih_identities""",
    f"""INSERT INTO ih_stats_rollup (space_id, service, metric, bucket_start, bucket_size, value)
        SELECT :space, '', 'm', now() - g * interval '1 hour', s, 1
        FROM generate_series(1, {N // 2}) g, (VALUES ('1h'), ('1m')) v(s)""",
    "ANALYZE ih_identities, ih_quota_usage, ih_operation_keys, ih_lease_logs, ih_lease_history, ih_events",
    "ANALYZE ih_webhook_deliveries, ih_notifications, ih_import_runs, ih_provider_operations",
    "ANALYZE ih_stats_metric_batches, ih_challenges, ih_stats_rollup",
]


async def test_cleanup_statements_use_indexes(hub: IdentityHub, space: SpaceHandle, db: AsyncEngine) -> None:
    """Ни один запрос чистки не читает таблицу целиком."""
    ref = await space.ref()
    await space.webhooks.create(
        url="https://hooks.example.test/plans", secret=SecretStr("s"), event_types=["identity.updated"]
    )
    async with db.begin() as conn:
        for statement in SEED:
            await conn.execute(text(statement), {"space": ref.id} if ":space" in statement else {})
    captured: list[tuple[str, Any]] = []

    def capture(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any) -> None:
        captured.append((statement, parameters))

    event.listen(db.sync_engine, "before_cursor_execute", capture)
    try:
        counts = await hub._store.cleanup_maintenance(RETENTION, CATEGORIES)
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert counts.keys() == CATEGORIES and not any(counts.values())
    deletes = [(statement, parameters) for statement, parameters in captured if "DELETE" in statement]
    tables = {statement.split("DELETE FROM ")[1].split()[0] for statement, _ in deletes}
    assert tables >= {
        "ih_quota_usage",
        "ih_operation_keys",
        "ih_lease_logs",
        "ih_lease_history",
        "ih_webhook_deliveries",
        "ih_notifications",
        "ih_events",
        "ih_import_runs",
        "ih_provider_operations",
        "ih_stats_metric_batches",
        "ih_challenges",
        "ih_stats_rollup",
    }
    async with db.connect() as conn:
        for statement, parameters in captured:
            if "set_config" in statement or "FROM ih_spaces" in statement:
                continue  # список пространств — справочник из нескольких строк
            rows = await conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            plan = "\n".join(str(row[0]) for row in rows)
            assert "Seq Scan" not in plan, f"{statement}\n{plan}"


async def test_each_batch_is_its_own_transaction(
    hub: IdentityHub, space: SpaceHandle, db: AsyncEngine
) -> None:
    for _ in range(5):
        assert await hub._store.apply_metric_rollup_batch(uuid4(), [])
    statements: list[str] = []

    def capture(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
        statements.append(statement)

    commits: list[int] = []
    event.listen(db.sync_engine, "before_cursor_execute", capture)
    event.listen(db.sync_engine, "commit", lambda _conn: commits.append(1))
    try:
        counts = await hub._store.cleanup_maintenance(
            {"metric_batches": timedelta(hours=-1)}, frozenset({"metric_batches"}), batch_size=2
        )
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", capture)

    assert counts == {"metric_batches": 5}
    assert len([statement for statement in statements if "DELETE" in statement]) == 3  # 2 + 2 + 1
    assert len(commits) == 3


async def test_claim_skips_delivery_of_missing_event(
    hub: IdentityHub, space: SpaceHandle, db: AsyncEngine
) -> None:
    """Доставка без события (осталась от прежних версий) не захватывается снова и снова."""
    endpoint = await space.webhooks.create(
        url="https://hooks.example.test/orphan", secret=SecretStr("s"), event_types=["identity.updated"]
    )
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO ih_webhook_deliveries (endpoint_id, event_id, status, attempt) "
                "VALUES (:endpoint, gen_random_uuid(), 'pending', 0)"
            ),
            {"endpoint": endpoint.id},
        )

    assert await hub._store.claim_webhook_deliveries(limit=10, claim_for=timedelta(seconds=30)) == []

    (delivery,) = await space.webhooks.deliveries(endpoint.id)
    assert delivery.attempt == 0
