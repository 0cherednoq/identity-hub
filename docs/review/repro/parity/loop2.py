import asyncio
from identity_hub.testing import MemoryStore
store = MemoryStore()
async def contend():
    space = await store.ensure_space("s")
    async def hold():
        async with store.transaction(space):
            await asyncio.sleep(0.01)   # реальная приостановка внутри транзакции
    await asyncio.gather(hold(), hold())
asyncio.run(contend())
try:
    asyncio.run(contend()); print("второй loop с конкуренцией: ok")
except Exception as exc:
    print("второй loop с конкуренцией:", type(exc).__name__, exc)
async def plain():
    space = await store.ensure_space("s")
    async with store.transaction(space) as tx:
        return await tx.now() is not None
try:
    print("второй loop без конкуренции:", asyncio.run(plain()))
except Exception as exc:
    print("второй loop без конкуренции:", type(exc).__name__, exc)
