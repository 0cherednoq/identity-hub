"""FR-6.6: общий сценарий shared-аренды для MemoryStore и PostgreSQL (D-140)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from identity_hub.enums import Lifecycle
from identity_hub.errors import ValidationFailed
from identity_hub.hub import IdentityHub
from identity_hub.service import LeaseMode
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

SHARED_TG = replace(FAKE_TG, name="imap", lease_mode=LeaseMode.SHARED)


async def check_parallel_shared_leases_keep_writing(hub: IdentityHub) -> None:
    """Параллельные shared-аренды одного аккаунта не вытесняют друг друга через fencing."""
    space = hub.space("shared-parallel", create=True)
    reg = await register(
        space, service=SHARED_TG, external_ref="box", session={"auth_key": "s0"}, lifecycle=Lifecycle.READY
    )
    pool = space.pools.define("mail", service=SHARED_TG)
    first, second = await pool.acquire(), await pool.acquire()
    assert first.identity_id == second.identity_id == reg.identity_id
    assert first.fencing == second.fencing

    async with first.operation("probe"):
        pass
    await first.save_session({"auth_key": "s1"})
    async with second.operation("probe"):
        pass
    await second.save_session({"auth_key": "s2"})
    await first.release()
    await second.release()


async def check_shared_rotates_least_recently_leased(hub: IdentityHub) -> None:
    """Выдача shared-аккаунтов идёт по давности последнего захвата, а не всегда один и тот же."""
    space = hub.space("shared-rotation", create=True)
    for ref in ("box-a", "box-b", "box-c"):
        await register(
            space, service=SHARED_TG, external_ref=ref, session={"auth_key": "s"}, lifecycle=Lifecycle.READY
        )
    pool = space.pools.define("mail", service=SHARED_TG)
    seen = []
    for _ in range(3):
        lease = await pool.acquire()
        seen.append(lease.identity_id)
        await lease.release()
    assert len(set(seen)) == 3
    lease = await pool.acquire()
    assert lease.identity_id == seen[0]
    await lease.release()


async def check_shared_rejects_idempotency_key(hub: IdentityHub) -> None:
    """Ключ идемпотентности не поддерживается в shared-режиме — явная ошибка вместо молчаливого игнора."""
    space = hub.space("shared-key", create=True)
    await register(
        space, service=SHARED_TG, external_ref="box-k", session={"auth_key": "s"}, lifecycle=Lifecycle.READY
    )
    with pytest.raises(ValidationFailed, match="shared"):
        await space.pools.define("mail", service=SHARED_TG).acquire(idempotency_key="job:1")
