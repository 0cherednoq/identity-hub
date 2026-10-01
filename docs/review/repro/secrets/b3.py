"""Token аренды в логе самой библиотеки: heartbeat падает с ошибкой БД -> logger.warning(exc_info=True)."""
import asyncio, io, logging
from datetime import timedelta
from sqlalchemy import text
from common import *
from identity_hub.errors import LeaseLost


async def main():
    hub, eng = await pg_hub()
    space = hub.space("s1", "S1"); iid = await make_identity(space)
    pool = space.pools.define("p", service="test_service", holder="w", ttl=timedelta(seconds=6),
                              heartbeat=timedelta(seconds=2), safety_margin=timedelta(seconds=2))
    buf = io.StringIO()
    h = logging.StreamHandler(buf); h.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    lg = logging.getLogger("identity_hub"); lg.addHandler(h); lg.setLevel(logging.WARNING)
    hub.lease_log_sink.setLevel(logging.WARNING)
    token = None
    try:
        async with pool.lease() as lease:
            token = str(lease._token)
            async with eng.connect() as locker:
                tr = await locker.begin()
                await locker.execute(text("select 1 from ih_leases for update"))
                # типичная защита продакшена: statement_timeout на роли/БД
                async with eng.begin() as c:
                    await c.execute(text("alter database ih set statement_timeout = '300ms'"))
                await eng.dispose()  # новые соединения подхватят настройку
                await asyncio.sleep(2.6)
                await tr.rollback()
            await asyncio.sleep(0.1)
    except LeaseLost as e:
        print("LeaseLost:", e)
    except Exception as e:
        print("exit exc:", type(e).__name__, str(e)[:100])
    finally:
        async with eng.begin() as c:
            await c.execute(text("alter database ih reset statement_timeout"))
        await eng.dispose()
    out = buf.getvalue()
    first = out.splitlines()[0] if out else ""
    print("first log line:", first)
    plines = [l for l in out.splitlines() if "[parameters:" in l]
    print("log line with SQL parameters:", (plines[0][:260] + "...") if plines else None)
    print("lease token present in library log output:", token in out, "| token =", token[:8] + "…")

asyncio.run(main())
