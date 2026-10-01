from common import *
from identity_hub.dispatch.notifications import TelegramRateLimited, TelegramRejected, _render
from identity_hub.rollups import BucketSize

class TG:
    """Имитация лимита Telegram: 20 сообщений в минуту в группу, 4096 символов."""
    def __init__(self, clock): self.clock = clock; self.sent = []; self.calls = 0; self.too_long = 0
    async def send_message(self, token, chat_id, text, *, thread_id=None):
        self.calls += 1
        if len(text) > 4096:
            self.too_long += 1; raise TelegramRejected(400, "HTTP 400: Bad Request: message is too long")
        now = self.clock.now()
        recent = [t for t in self.sent if (now - t).total_seconds() < 60]
        if len(recent) >= 20:
            raise TelegramRateLimited(60 - (now - recent[0]).total_seconds() + 1)
        self.sent.append(now)

async def setup(n, digest=None):
    svc = make_service("svc")
    clock = FakeClock(); tg = TG(clock)
    hub, clock = mkhub([svc], clock=clock, telegram_transport=tg); await hub.start(); sp = hub.space("s")
    ch = await sp.notifications.create_telegram_channel("ops", bot_token=SecretStr("123:abc"), chat_id=-100)
    await sp.notifications.create_rule(name="bans", channel_id=ch.id, event_types=["identity.standing.changed"], digest_seconds=digest, created_by="op")
    ids = [await make_identity(sp, service=svc) for _ in range(n)]
    for i in ids:
        await sp.identities.override(i, Override("standing", "", "banned", reason="mass ban", actor="platform"))
    return hub, clock, tg, sp

def statuses(hub):
    from collections import Counter
    return dict(Counter(r["status"] for r in hub.store._state.notifications.values()))

async def n1_burst():
    hub, clock, tg, sp = await setup(300)
    for rnd in range(40):
        await hub.dispatcher.drain()
        clock.advance(65)
    print("  300 bans, no digest ->", statuses(hub), "delivered:", len(tg.sent), "API calls:", tg.calls)

async def n2_digest():
    hub, clock, tg, sp = await setup(150, digest=300)
    for rnd in range(10):
        await hub.dispatcher.drain(); clock.advance(301)
    row = next(iter(hub.store._state.notifications.values()))
    print("  150 bans, digest 300s ->", statuses(hub), "events in digest:", len(row["event_ids"]), "error:", row["error"], "too_long:", tg.too_long)

async def r1_rebuild_and_cost():
    svc = make_service("svc"); hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    reads = []
    orig = hub.store.rollup_source_events
    async def spy(slug, start, end):
        r = await orig(slug, start, end); reads.append(len(r)); return r
    hub.store.rollup_source_events = spy
    pool = sp.pools.define("p", service=svc, holder="w")
    iid = await make_identity(sp, service=svc)
    for i in range(300):
        async with pool.lease(for_operation="work") as lease:
            async with lease.operation("work"): pass
        if i % 50 == 49:
            await hub.dispatcher.drain()
    total_events = len(hub.store._state.events)
    print(f"  events={total_events}, rollup batches={len(reads)}, rows re-read from ih_events={sum(reads)} (sizes {reads[:8]}...)")
    tele = [k for k in hub.store._state.rollups if k.metric.startswith("telemetry.")]
    print("  telemetry rollup rows before rebuild:", len(tele))
    n = await hub.rollups.rebuild("s", clock.now() - timedelta(hours=1), clock.now() + timedelta(hours=1))
    tele = [k for k in hub.store._state.rollups if k.metric.startswith("telemetry.")]
    print("  after rollups.rebuild: telemetry rows:", len(tele), "event rows:", n)

async def m1_buffer_growth():
    svc = make_service("svc"); hub, clock = mkhub([svc]); await hub.start(); sp = hub.space("s")
    pool = sp.pools.define("p", service=svc, holder="w")
    await make_identity(sp, service=svc)
    for minute in range(120):
        async with pool.lease(for_operation="work") as lease:
            async with lease.operation("work"): pass
        clock.advance(60)
    print("  worker process w/o dispatcher, 120 min: pending metric keys =", len(hub.metric_rollups._pending))

async def main():
    await section("N1 burst without digest", n1_burst())
    await section("N2 large digest", n2_digest())
    await section("R1 rollups cost / rebuild", r1_rebuild_and_cost())
    await section("M1 metric buffer growth", m1_buffer_growth())
asyncio.run(main())
