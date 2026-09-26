"""P2.6: связи и read-модели (ТЗ FR-4.1, 4.2, 4.4)."""

from __future__ import annotations

from uuid import UUID

import pytest

from identity_hub._ids import new_id
from identity_hub.errors import AlreadyExists, NotFound, ValidationFailed
from identity_hub.hub import IdentityHub, SpaceHandle

pytestmark = pytest.mark.pg


async def _reg(space: SpaceHandle, ref: str, service: str = "fake_tg") -> UUID:
    return (await space.register(service=service, external_ref=ref)).identity_id


async def test_link_unlink_and_views(space: SpaceHandle) -> None:
    mail = await _reg(space, "m@x", "fake_exchange")
    phone = await _reg(space, "+100")
    tg = await _reg(space, "+1")
    await space.link(tg, "recovery_email", mail)
    await space.link(tg, "registered_phone", phone)
    with pytest.raises(AlreadyExists):
        await space.link(tg, "recovery_email", phone)

    links = await space.links_of(tg)
    assert [(link.role, link.identity_id) for link in links.outgoing] == [
        ("recovery_email", mail),
        ("registered_phone", phone),
    ]
    assert links.linked("recovery_email") is not None
    assert links.linked("recovery_email").external_ref == "m@x"  # type: ignore[union-attr]
    assert links.incoming == []
    mail_links = await space.links_of(mail)
    assert [(link.role, link.identity_id, link.service) for link in mail_links.incoming] == [
        ("recovery_email", tg, "fake_tg")
    ]

    summary = await space.link_summary([tg, mail, phone, new_id()])
    assert summary[tg].outgoing == 2 and summary[tg].roles == ["recovery_email", "registered_phone"]
    assert summary[mail].incoming == 1 and summary[mail].outgoing == 0

    assert await space.unlink(tg, "recovery_email")
    assert not await space.unlink(tg, "recovery_email")
    assert (await space.link_summary([mail]))[mail].incoming == 0


async def test_link_validation(hub: IdentityHub, space: SpaceHandle) -> None:
    a = await _reg(space, "+1")
    foreign = await _reg(hub.space("other"), "+2")
    with pytest.raises(ValidationFailed):
        await space.link(a, "x", a)
    with pytest.raises(NotFound):
        await space.link(a, "x", foreign)
    with pytest.raises(NotFound):
        await space.link(foreign, "x", a)


async def test_impact_transitive(space: SpaceHandle) -> None:
    # gmail <- instagram <- tiktok; gmail <- telegram; цикл tiktok -> gmail не зацикливает обход
    gmail = await _reg(space, "g@x", "fake_exchange")
    insta = await _reg(space, "insta")
    tiktok = await _reg(space, "tiktok")
    tg = await _reg(space, "+1")
    await space.link(insta, "recovery_email", gmail)
    await space.link(tiktok, "login_via", insta)
    await space.link(tg, "recovery_email", gmail)
    await space.link(gmail, "recovery_phone", tiktok)

    impact = await space.impact_of(gmail)
    assert [(i.identity_id, i.depth) for i in impact][:2] in (
        [(insta, 1), (tg, 1)],
        [(tg, 1), (insta, 1)],
    )
    deep = {i.identity_id: i for i in impact}
    assert deep[tiktok].depth == 2
    assert deep[tiktok].path == ["recovery_email", "login_via"]
    assert gmail not in deep
    shallow = await space.impact_of(gmail, max_depth=1)
    assert {i.identity_id for i in shallow} == {insta, tg}
