import asyncio
from datetime import timedelta

from identity_hub import IdentityHub, OperationRule, Quota, Service, StaticSecretsProvider
from identity_hub.testing import MemoryStore, make_identity

SVC = Service(name="svc", title="Svc", operations={"send": OperationRule(quotas=(Quota(5, timedelta(hours=1)),))})


async def main() -> None:
    hub = IdentityHub(store=MemoryStore(), secrets=StaticSecretsProvider({"k": bytes(32)}, active="k"), services=[SVC])
    await hub.start()
    space = hub.space("s")
    await make_identity(space, service="svc")
    pool = space.pools.define("p", service="svc")

    # 1) первая попытка падает до/во время вызова платформы
    try:
        async with pool.lease() as lease:
            async with lease.operation("send", key="job:1") as op:
                print("attempt 1: duplicate =", op.duplicate)
                raise ConnectionError("сеть упала, сообщение НЕ отправлено")
    except ConnectionError:
        pass

    # 2) ретрай той же задачи с тем же ключом
    async with pool.lease() as lease:
        async with lease.operation("send", key="job:1") as op:
            print("attempt 2 (после неуспеха): duplicate =", op.duplicate, "| поля Operation:", [a for a in vars(op)])

    # 3) опечатка в slug пространства
    typo = hub.space("prodution")
    print("space с опечаткой, count =", await typo.identities.count(), "| spaces:", [s.slug for s in await hub.list_spaces()])


asyncio.run(main())
