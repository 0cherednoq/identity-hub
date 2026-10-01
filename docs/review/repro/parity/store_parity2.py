import asyncio
from datetime import UTC, datetime, timedelta
from harness import PAYLOAD, ident, run_scenarios
from identity_hub.events import EventDraft
from identity_hub.store.base import CandidateQuery, LeaseCounters, QuotaCharge
from store_parity import lease, tick, Q, _collect

async def idem_key_two_identities(store, space, clock):
    a, b = ident("a"), ident("b")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a); await tx.insert_identity(b)
        await lease(tx, a.id, key="k")
    try:
        async with store.transaction(space) as tx:
            await lease(tx, b.id, key="k")
        return "second lease with same key created"
    except Exception as exc:
        return type(exc).__name__

async def lease_lifecycle_after_expiry(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        got = await lease(tx, a.id, ttl=1, key="k")
    await tick(store, clock, 1.2)
    async with store.transaction(space) as tx:
        ext = await tx.extend_lease(got, 30, LeaseCounters.empty(), shared=False)
        rev = await tx.revoke_lease(a.id)
        live = await tx.find_live_lease("k")
        cand = await tx.pick_candidate(Q)
        rel = await tx.release_lease(got, "released", LeaseCounters.empty(), shared=False)
        again = await tx.release_lease(got, "released", LeaseCounters.empty(), shared=False)
        hist = await tx.lease_history(a.id, limit=5, before=None)
        fencing = await tx.current_fencing(a.id)
    return ext, rev is None, live is None, cand is not None, rel, again, [h.end_reason for h in hist], fencing

async def reacquire_reports_reclaimed(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        await lease(tx, a.id, ttl=1)
    await tick(store, clock, 1.2)
    async with store.transaction(space) as tx:
        cur = await tx.get_identity(a.id)
        new, reclaimed = await tx.acquire_lease(cur, pool="p", holder="h", ttl_seconds=30, idempotency_key=None, shared=False)
        hist = await tx.lease_history(a.id, limit=5, before=None)
    return new.fencing, reclaimed is not None, sorted(str(h.end_reason) for h in hist), [h.duration is not None for h in hist if h.end_reason]

async def shared_leases(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        l1 = await lease(tx, a.id, shared=True); l2 = await lease(tx, a.id, shared=True)
        q = CandidateQuery("svc", frozenset({"good"}), shared=True)
        res = (l1.fencing, l2.fencing, await tx.count_candidates(q, 10), await tx.count_live_candidates(q, 10),
               (await tx.current_lease(a.id)) is None,
               await tx.extend_lease(l1, 30, LeaseCounters.empty(), shared=True) is not None,
               await tx.release_lease(l1, "released", LeaseCounters.empty(), shared=True),
               await tx.extend_lease(l1, 30, LeaseCounters.empty(), shared=True) is not None)
    return res

async def cleanup_boundaries(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
        got = await lease(tx, a.id)
        await tx.charge_quotas(a.id, [QuotaCharge("w", 1, datetime(2020,1,1,tzinfo=UTC), 5)], operation_key="k1", kind="w")
        await tx.emit([EventDraft("identity.registered", "t", service="svc", identity_id=a.id)])
    async with store.transaction(space) as tx:
        await tx.release_lease(got, "released", LeaseCounters.empty(), shared=False)
        b = ident("b"); await tx.insert_identity(b); await lease(tx, b.id)   # открытая аренда
    await tick(store, clock, 2.2)
    keep = {k: timedelta(days=1) for k in ("events","lease_history","lease_logs","deliveries","operation_keys","import_runs","provider_operations")}
    drop = {k: timedelta(seconds=1) for k in keep}
    names = frozenset({"quota_windows","operation_keys","lease_history","events","lease_logs","webhook_deliveries","notifications","import_runs","provider_operations"})
    first = await store.cleanup_maintenance(keep, names)
    second = await store.cleanup_maintenance(drop, names)
    return dict(sorted(first.items())), dict(sorted(second.items()))

async def rollback_reverts_everything(store, space, clock):
    a = ident("a")
    async with store.transaction(space) as tx:
        await tx.insert_identity(a)
    try:
        async with store.transaction(space) as tx:
            await lease(tx, a.id, key="k")
            await tx.charge_quotas(a.id, [QuotaCharge("w", 3600, datetime(2020,1,1,tzinfo=UTC), 5)], operation_key="op", kind="w")
            await tx.emit([EventDraft("identity.registered", "t", service="svc", identity_id=a.id)])
            await tx.put_credentials(a.id, PAYLOAD)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    seen = []
    await store.process_events("c", lambda evs: _collect(seen, evs), limit=10, external_only=False)
    async with store.transaction(space) as tx:
        row = await tx.get_identity(a.id)
        return (row.fencing_seq, row.event_seq, await tx.current_lease(a.id) is None, await tx.quota_usage(a.id),
                await tx.get_credentials(a.id) is None, len(seen),
                (await tx.charge_quotas(a.id, [], operation_key="op", kind="w")).duplicate)

if __name__ == "__main__":
    asyncio.run(run_scenarios([idem_key_two_identities, lease_lifecycle_after_expiry, reacquire_reports_reclaimed, shared_leases, cleanup_boundaries, rollback_reverts_everything]))
