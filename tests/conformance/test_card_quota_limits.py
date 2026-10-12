"""C3.4: карточка аккаунта показывает лимит квоты рядом с расходом."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.fixtures.api import register
from tests.fixtures.services import FAKE_TG

from identity_hub import IdentityHub, OperationRule, Quota, SpaceHandle

pytestmark = pytest.mark.conformance

HOUR, DAY = 3600, 86400


async def test_card_shows_limit_next_to_usage(space: SpaceHandle) -> None:
    """C3.4: у каждого окна — `used` и `limit` из правила операции сервиса."""
    reg = await register(
        space, service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    pool = space.pools.define("p", service="fake_tg")
    async with pool.lease(among=[reg.identity_id]) as lease:
        async with lease.operation("send_message"):
            pass
        async with lease.operation("send_message"):
            pass
        async with lease.operation("read_channel"):
            pass

    card = await space.identities.card(reg.identity_id)

    windows = {(q.kind, q.window_seconds): (q.used, q.limit) for q in card.quotas}
    assert windows == {
        ("send_message", HOUR): (2, 20),
        ("send_message", DAY): (2, 100),
        ("read_channel", HOUR): (1, 100),
    }


async def test_limit_follows_service_rules(hub_under_test: IdentityHub, space: SpaceHandle) -> None:
    """C3.4: лимит берётся из описания сервиса при чтении: смена правила видна без записи в БД."""
    reg = await register(
        space, service="fake_tg", external_ref="+1", lifecycle="ready", session={"auth_key": "k"}
    )
    lease = await space.pools.define("p", service="fake_tg").acquire(among=[reg.identity_id])
    async with lease.operation("send_message"):
        pass
    await lease.release()

    stricter = OperationRule(
        standing=FAKE_TG.rule("send_message").standing,
        quotas=(Quota(5, timedelta(hours=1)),),  # часовой лимит другой, дневного окна больше нет
    )
    hub_under_test._services["fake_tg"] = replace(
        FAKE_TG, operations={**FAKE_TG.operations, "send_message": stricter}
    )
    try:
        card = await space.identities.card(reg.identity_id)
    finally:
        hub_under_test._services["fake_tg"] = FAKE_TG

    limits = {(q.kind, q.window_seconds): q.limit for q in card.quotas}
    assert limits[("send_message", HOUR)] == 5
    assert limits[("send_message", DAY)] is None  # правила с таким окном больше нет
