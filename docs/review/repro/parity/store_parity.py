"""Сценарии уровня StoreTx/Store: один и тот же код на MemoryStore и PgStore."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from harness import PAYLOAD, ident, run_scenarios
from sqlalchemy import select

from identity_hub._ids import new_id
from identity_hub.events import EventDraft
from identity_hub.store.base import (
    CandidateQuery,
    EventReadFilter,
    LeaseCounters,
    NewNotificationChannel,
    NewNotificationRule,
    NewProxy,
    NewWebhookEndpoint,
    NotificationEnqueue,
    QuotaCharge,
    SearchFilter,
)

Q = CandidateQuery("svc", frozenset({"good"}))


async def lease(tx, row_id, *, shared=False, ttl=30, key=None, proxy_id=None):
    cur = await tx.get_identity(row_id)
    return (
        await tx.acquire_lease(
            cur, pool="p", holder="h", ttl_seconds=ttl, idempotency_key=key, shared=shared, proxy_id=proxy_id
        )
    )[0]


async def tick(store, clock, seconds):
    """Сдвинуть время: FakeClock для памяти, реальный sleep для PG."""
    if clock is not None:
        clock.advance(seconds)
    else:
        await asyncio.sleep(seconds)


# ---------------------------------------------------------------- связи


async def dependents_path_order(store, space, clock):
    a, b, c = ident("a"), ident("b"), ident("c")
    async with store.transaction(space) as tx:
        for r in (a, b, c):
            await tx.insert_identity(r)
        await tx.insert_link(b.id, "r1", a.id)
        await tx.insert_link(c.id, "r2", b.id)
        deps = await tx.dependents(a.id, 5)
    names = {a.id: "a", b.id: "b", c.id: "c"}
    return [(names[i], d, p) for i, d, p in deps]


async def insert_link_missing_target(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        return await tx.insert_link(a.id, "r1", uuid4())


async def links_of_order_and_created_at(store, space, clock):
    a, b, c = ident("a"), ident("b"), ident("c")
    async with store.transaction(space) as tx:
        for r in (a, b, c):
            await tx.insert_identity(r)
    await tick(store, clock, 1.1)
    async with store.transaction(space) as tx:
        await tx.insert_link(a.id, "zz", b.id)
        await tx.insert_link(a.id, "aa", c.id)
        out, _ = await tx.links_of(a.id)
        ident_created = (await tx.get_identity(b.id)).created_at
    return [r.role for r in out], [r.created_at == ident_created for r in out]


# ---------------------------------------------------------------- аренда


async def lease_stats_open_lease(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        await lease(tx, a.id)
        stats = await tx.lease_stats(a.id, datetime(2000, 1, 1, tzinfo=UTC))
    return stats.count, stats.by_reason


async def lru_after_shared_history(store, space, clock):
    first, second = sorted([new_id(), new_id()])
    a, b = ident("a", id=first), ident("b", id=second)
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        await tx.insert_identity(b)
        await lease(tx, a.id, shared=True)
    async with store.transaction(space) as tx:
        picked = await tx.pick_candidate(Q)
    return "a(lower id)" if picked.id == a.id else "b"


async def candidate_labels_nested(store, space, clock):
    a = ident("a", labels={"geo": {"country": "ru", "city": "msk"}, "ids": [1, 2]})
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        nested = await tx.pick_candidate(replace(Q, labels={"geo": {"country": "ru"}}))
        arr = await tx.pick_candidate(replace(Q, labels={"ids": [1]}))
        null = await tx.pick_candidate(replace(Q, labels={"missing": None}))
    return {"nested": nested is not None, "array": arr is not None, "missing=None": null is not None}


async def candidate_among_select(store, space, clock):
    from identity_hub.postgres.read_schema import ReadSchema

    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        ids = ReadSchema(None).identities
        picked = await tx.pick_candidate(replace(Q, among=select(ids.c.id).where(ids.c.external_ref == "a")))
    return picked is not None


async def candidate_quarantined_with_expired_until(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        now = await tx.now()
        await tx.update_identity(
            a.id, {"availability": "quarantined", "availability_until": now - timedelta(seconds=5)}
        )
        picked = await tx.pick_candidate(Q)
    return picked is not None


async def expire_leases_event_and_count(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        await lease(tx, a.id, ttl=1)
    await tick(store, clock, 1.2)
    async with store.transaction(space) as tx:
        n = await tx.expire_leases(10)
        again = await tx.expire_leases(10)
    events = []
    await store.process_events("c-exp", lambda evs: _collect(events, evs), limit=50, external_only=False)
    data = [sorted(e.data) for e in events if e.type == "lease.expired" and e.space == space.slug]
    return n, again, data


async def _collect(target, events):
    target.extend(events)


async def expire_leases_limit_starvation(store, space, clock):
    """3 аккаунта отработали и освободились, 4-й бросил аренду. limit=2."""
    rows = [ident(f"r{i}") for i in range(4)]
    async with store.transaction(space) as tx:
        for r in rows:
            await tx.insert_identity(r)
    for r in rows[:3]:
        async with store.transaction(space) as tx:
            got = await lease(tx, r.id)
        async with store.transaction(space) as tx:
            await tx.release_lease(got, "released", LeaseCounters.empty(), shared=False)
    async with store.transaction(space) as tx:
        await lease(tx, rows[3].id, ttl=1)
    await tick(store, clock, 1.2)
    results = []
    for _ in range(3):
        async with store.transaction(space) as tx:
            results.append(await tx.expire_leases(2))
    async with store.transaction(space) as tx:
        hist = await tx.lease_history(rows[3].id, limit=1, before=None)
    return results, hist[0].end_reason


async def lease_read_expires_at_after_release(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        got = await lease(tx, a.id)
    async with store.transaction(space) as tx:
        await tx.release_lease(got, "released", LeaseCounters.empty(), shared=False)
        page = await tx.lease_read_page(limit=5, before=None, pool=None, service=None, holder=None)
    return [row.expires_at is None for row in page]


async def lease_page_order_same_timestamp(store, space, clock):
    rows = [ident(f"r{i}") for i in range(6)]
    async with store.transaction(space) as tx:
        made = []
        for r in rows:
            await tx.insert_identity(r)
            made.append((await lease(tx, r.id)).lease_id)
        page = await tx.lease_read_page(limit=10, before=None, pool=None, service=None, holder=None)
    return [made.index(r.lease_id) for r in page]


# ---------------------------------------------------------------- события


async def event_page_order_same_timestamp(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        ids = await tx.emit(
            [EventDraft("identity.registered", "t", service="svc", identity_id=a.id) for _ in range(6)]
        )
    async with store.transaction(space) as tx:
        page = await tx.event_read_page(
            EventReadFilter(frozenset({"identity.registered"})), limit=10, before=None
        )
    return [r.sequence for r in page]


# ---------------------------------------------------------------- проверки, секреты


async def verification_evidence_survives_second_save(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        await tx.save_verification(a.id, "kyc", {"attempts": 1}, PAYLOAD)
        await tx.save_verification(a.id, "kyc", {"attempts": 2})
        row = await tx.load_verification(a.id, "kyc")
    return type(row.evidence).__name__, row.attempts


async def access_metadata_versions(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        before = await tx.access_metadata(a.id)
        await tx.put_credentials(a.id, PAYLOAD)
        await tx.put_credentials(a.id, PAYLOAD)
        await tx.put_session(a.id, "default", PAYLOAD, fencing=0)
        await tx.put_session(a.id, "default", PAYLOAD, fencing=0)
        cred, sessions = await tx.access_metadata(a.id)
    return (
        ("before", before[0].present, before[0].version),
        ("cred", cred.version, cred.updated_at is not None),
        ("session", sessions[0].version, sessions[0].updated_at is not None),
    )


async def stale_identities_skips_retired(store, space, clock):
    a, b = ident("a"), ident("b", lifecycle="retired")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        await tx.insert_identity(b)
        stale = await tx.stale_identities("svc", "standing", "", 10, 10)
    return sorted("a" if i == a.id else "b(retired)" for i in stale)


# ---------------------------------------------------------------- прокси


async def delete_identity_frees_proxy_capacity(store, space, clock):
    a, b = ident("a"), ident("b")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        await tx.insert_identity(b)
        proxy_id, _ = await tx.insert_proxy(NewProxy(new_id(), "res", "http", "h", 80, "fp", 1, PAYLOAD))
        first = await tx.choose_proxy("res", None, claim=True)
        await tx.bind_proxy(a.id, first.id, None)
        await tx.delete_identity(a.id)
        second = await tx.choose_proxy("res", None, claim=True)
    issues = [i.kind for i in await store.doctor(fix=False) if i.space == space.slug]
    return second is not None, issues


# ---------------------------------------------------------------- квоты


async def quota_window_before_origin(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        now = await tx.now()
        origin = now + timedelta(seconds=90.5)
        out = await tx.charge_quotas(
            a.id, [QuotaCharge("w", 60, origin, 1)], operation_key=None, kind="w"
        )
        usage = await tx.quota_usage(a.id)
    return [(round((u.window_start - now).total_seconds(), 1)) for u in usage], out.ok


async def quota_key_released_when_exhausted(store, space, clock):
    a = ident("a")
    origin = datetime(2020, 1, 1, tzinfo=UTC)
    ch = [QuotaCharge("w", 86400, origin, 1)]
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        r1 = await tx.charge_quotas(a.id, ch, operation_key="k1", kind="w")
        r2 = await tx.charge_quotas(a.id, ch, operation_key="k2", kind="w")
        r3 = await tx.charge_quotas(a.id, ch, operation_key="k2", kind="w")
    return (r1.ok, r1.duplicate), (r2.ok, r2.duplicate, r2.exhausted.used), (r3.ok, r3.duplicate)


# ---------------------------------------------------------------- поиск


async def search_short_id_and_sort(store, space, clock):
    refs = ["bob", "Alice", "_under", "Zed", "alice2", "10", "9"]
    rows = [ident(r, profile={"n": v}) for r, v in zip(refs, [10, 9, 1.5, True, "x", None, 100])]
    async with store.transaction(space) as tx:
        for r in rows:
            await tx.insert_identity(r)
        by_ref, _ = await tx.search_identities(
            SearchFilter(sort_by="external_ref", sort_desc=False), limit=20, after=None, count_limit=None
        )
        by_profile, _ = await tx.search_identities(
            SearchFilter(sort_by="profile:n", sort_desc=False), limit=20, after=None, count_limit=None
        )
        hex16 = rows[0].id.hex[-16:]
        short, _ = await tx.search_identities(
            SearchFilter(short_id=hex16), limit=5, after=None, count_limit=None
        )
        upper, _ = await tx.search_identities(
            SearchFilter(short_id=rows[0].id.hex[-8:].upper()), limit=5, after=None, count_limit=None
        )
        contains, _ = await tx.search_identities(
            SearchFilter(contains=(("profile", ({"n": 10},)),)), limit=5, after=None, count_limit=None
        )
    return {
        "by_ref": [r.external_ref for r in by_ref],
        "by_profile": [r.profile.get("n") for r in by_profile],
        "short16": len(short),
        "short8_upper": len(upper),
        "contains": len(contains),
    }


async def search_default_order_one_tx(store, space, clock):
    rows = [ident(f"r{i}", id=uuid4()) for i in range(6)]
    async with store.transaction(space) as tx:
        for r in rows:
            await tx.insert_identity(r)
    async with store.transaction(space) as tx:
        found, _ = await tx.search_identities(SearchFilter(), limit=20, after=None, count_limit=None)
    return [r.external_ref for r in found] == [r.external_ref for r in sorted(rows, key=lambda x: x.id, reverse=True)]


# ---------------------------------------------------------------- уведомления и вебхуки


async def notification_rules_order_and_missing_channel(store, space, clock):
    async with store.transaction(space) as tx:
        ch = await tx.insert_notification_channel(NewNotificationChannel(new_id(), "t", {}, PAYLOAD))
    names = []
    for name in ("zeta", "alpha"):
        await tick(store, clock, 0.05)
        async with store.transaction(space) as tx:
            await tx.insert_notification_rule(
                NewNotificationRule(new_id(), ch.id, name, ["*"], "info", {}, None, None, "t")
            )
    async with store.transaction(space) as tx:
        names = [r.name for r in await tx.notification_rules()]
    try:
        async with store.transaction(space) as tx:
            await tx.insert_notification_rule(
                NewNotificationRule(new_id(), uuid4(), "x", ["*"], "info", {}, None, None, "t")
            )
        err = "no error"
    except Exception as exc:  # noqa: BLE001
        err = type(exc).__name__
    return names, err


async def _event_ids(store, space, n):
    a = ident(f"ev-{uuid4().hex[:6]}")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        return await tx.emit(
            [EventDraft("identity.registered", "t", service="svc", identity_id=a.id) for _ in range(n)]
        )


async def notification_finish_sets_sent_at(store, space, clock):
    async with store.transaction(space) as tx:
        ch = await tx.insert_notification_channel(NewNotificationChannel(new_id(), "t", {}, PAYLOAD))
        rule = await tx.insert_notification_rule(
            NewNotificationRule(new_id(), ch.id, "r", ["*"], "info", {}, None, None, "t")
        )
        now = await tx.now()
    (eid,) = await _event_ids(store, space, 1)
    item = NotificationEnqueue(rule.id, ch.id, eid, None, now - timedelta(seconds=1))
    n1 = await store.enqueue_notifications([item])
    n2 = await store.enqueue_notifications([item])  # повтор после сбоя диспетчера
    attempts = await store.claim_notifications(limit=10, claim_for=timedelta(seconds=30))
    await store.finish_notification(
        attempts[0].notification_id, attempts[0].attempt, succeeded=True, error=None,
        retry_after=timedelta(seconds=5), max_attempts=3,
    )
    async with store.transaction(space) as tx:
        page = await tx.notification_delivery_read_page(limit=10)
    snap = await store.metrics_snapshot()
    return {
        "enqueue": (n1, n2),
        "rows": [(r.status, r.sent_at is not None) for r in page],
        "last_success": snap.delivery_last_success_at["notifications"] is not None,
    }


async def webhook_claim_starved_by_disabled_endpoint(store, space, clock):
    async with store.transaction(space) as tx:
        off = await tx.insert_webhook_endpoint(NewWebhookEndpoint(new_id(), "https://a", None, [], {}, "info", PAYLOAD))
        on = await tx.insert_webhook_endpoint(NewWebhookEndpoint(new_id(), "https://b", None, [], {}, "info", PAYLOAD))
    ids = await _event_ids(store, space, 2)
    await store.enqueue_webhook_deliveries([(off.id, ids[0]), (off.id, ids[1])])
    await store.enqueue_webhook_deliveries([(on.id, ids[0])])
    async with store.transaction(space) as tx:
        await tx.set_webhook_enabled(off.id, False, "manual")
    await tick(store, clock, 0.05)
    claimed = await store.claim_webhook_deliveries(limit=2, claim_for=timedelta(seconds=30))
    return len(claimed)


async def webhook_success_keeps_manual_disable_reason(store, space, clock):
    async with store.transaction(space) as tx:
        ep = await tx.insert_webhook_endpoint(NewWebhookEndpoint(new_id(), "https://a", None, [], {}, "info", PAYLOAD))
    ids = await _event_ids(store, space, 1)
    await store.enqueue_webhook_deliveries([(ep.id, ids[0])])
    await tick(store, clock, 0.05)
    (att,) = await store.claim_webhook_deliveries(limit=2, claim_for=timedelta(seconds=30))
    async with store.transaction(space) as tx:
        await tx.set_webhook_enabled(ep.id, False, "manual")
    await store.finish_webhook_delivery(
        att.delivery_id, att.attempt, succeeded=True, response_code=200, response_ms=1, error=None,
        retry_after=timedelta(seconds=1), max_failures=3,
    )
    async with store.transaction(space) as tx:
        row = await tx.get_webhook_endpoint(ep.id)
    return row.enabled, row.disabled_reason


# ---------------------------------------------------------------- транзакции


async def nested_transaction(store, space, clock):
    async with store.transaction(space) as outer:
        await outer.insert_identity(ident("a"))

        async def inner():
            async with store.transaction(space) as tx:
                return await tx.now() is not None

        try:
            return await asyncio.wait_for(inner(), 2)
        except TimeoutError:
            return "DEADLOCK (timeout 2s)"


async def due_timers_limit(store, space, clock):
    rows = [ident(f"r{i}") for i in range(3)]
    async with store.transaction(space) as tx:
        now = await tx.now()
        for i, r in enumerate(rows):
            await tx.insert_identity(r)
            await tx.update_identity(
                r.id,
                {
                    "availability": "paused",
                    "availability_until": now - timedelta(seconds=10 + i),
                    "standing_until": now - timedelta(seconds=20 + i),
                },
            )
        due = await tx.due_identity_timers(2)
    return len(due), [region for _, region in due]


async def metrics_cursor_updated_at(store, space, clock):
    await _event_ids(store, space, 1)
    await store.process_events("c-metrics", lambda evs: _collect([], evs), limit=10)
    snap = await store.metrics_snapshot()
    return "c-metrics" in snap.cursor_updated_at


async def notification_digest_redelivery(store, space, clock):
    async with store.transaction(space) as tx:
        ch = await tx.insert_notification_channel(NewNotificationChannel(new_id(), "t", {}, PAYLOAD))
        rule = await tx.insert_notification_rule(
            NewNotificationRule(new_id(), ch.id, "r", ["*"], "info", {}, 60, None, "t")
        )
        now = await tx.now()
    ids = await _event_ids(store, space, 2)
    items = [NotificationEnqueue(rule.id, ch.id, e, f"{rule.id}:d", now + timedelta(seconds=60)) for e in ids]
    first = await store.enqueue_notifications(items)
    again = await store.enqueue_notifications(items)  # повтор пачки после сбоя потребителя
    async with store.transaction(space) as tx:
        page = await tx.notification_delivery_read_page(limit=10)
    return first, again, [len(r.event_ids) for r in page]


SCENARIOS = [
    notification_digest_redelivery,
    dependents_path_order,
    insert_link_missing_target,
    links_of_order_and_created_at,
    lease_stats_open_lease,
    lru_after_shared_history,
    candidate_labels_nested,
    candidate_among_select,
    candidate_quarantined_with_expired_until,
    expire_leases_event_and_count,
    expire_leases_limit_starvation,
    lease_read_expires_at_after_release,
    lease_page_order_same_timestamp,
    event_page_order_same_timestamp,
    verification_evidence_survives_second_save,
    access_metadata_versions,
    stale_identities_skips_retired,
    delete_identity_frees_proxy_capacity,
    quota_window_before_origin,
    quota_key_released_when_exhausted,
    search_short_id_and_sort,
    search_default_order_one_tx,
    notification_rules_order_and_missing_channel,
    notification_finish_sets_sent_at,
    webhook_claim_starved_by_disabled_endpoint,
    webhook_success_keeps_manual_disable_reason,
    nested_transaction,
    due_timers_limit,
    metrics_cursor_updated_at,
]

if __name__ == "__main__":
    asyncio.run(run_scenarios(SCENARIOS))
