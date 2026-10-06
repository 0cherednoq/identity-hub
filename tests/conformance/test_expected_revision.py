"""IMP-07 (I1.2b): `expected_revision` в командах изменения `space.*` (D-205, D-206)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from identity_hub import (
    ChangeSource,
    IdentityCreate,
    ImportRecord,
    Observation,
    Override,
    RevisionConflict,
    SpaceHandle,
    Standing,
    ValidationFailed,
)
from identity_hub.read.events import EventFilters

pytestmark = [pytest.mark.anyio, pytest.mark.conformance]

Command = Callable[[SpaceHandle, UUID, int | None], Awaitable[Any]]


@dataclass(frozen=True)
class Case:
    """Команда изменения; `prepare` готовит аккаунт так, чтобы команда реально его меняла."""

    command: Command
    prepare: Callable[[SpaceHandle], Awaitable[UUID]] | None = None


async def _plain(space: SpaceHandle) -> UUID:
    created = await space.identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref=f"+{uuid4().hex[:12]}",
            lifecycle="ready",
            credentials={"phone": "+1", "password": "p"},
            session={"auth_key": "k"},
        ),
        actor="test",
    )
    return created.id


async def _banned(space: SpaceHandle) -> UUID:
    identity_id = await _plain(space)
    await space.identities.override(
        identity_id, Override(region="standing", key="", state=Standing.BANNED, reason="r", actor="admin")
    )
    return identity_id


async def _pinned(space: SpaceHandle) -> UUID:
    identity_id = await _plain(space)
    await space.identities.override(
        identity_id,
        Override(
            region="standing",
            key="",
            state=Standing.LIMITED,
            reason="r",
            actor="admin",
            pin_for=timedelta(hours=1),
        ),
    )
    return identity_id


async def _with_requirement(space: SpaceHandle) -> UUID:
    identity_id = await _plain(space)
    await space.verifications.requirements.add(identity_id, "selfie", actor="platform")
    return identity_id


async def _linked(space: SpaceHandle) -> UUID:
    identity_id = await _plain(space)
    await space.links.add(identity_id, "recovery", await _plain(space), actor="ops")
    return identity_id


async def _under_review(space: SpaceHandle) -> UUID:
    report = await space.imports.run(
        service="fake_exchange",
        records=[
            ImportRecord(external_ref=f"ex-{uuid4().hex[:12]}", verifications={"kyc_id": "under_review"})
        ],
        actor="test",
    )
    return report.created[0]


async def _link_target(space: SpaceHandle, identity_id: UUID, rev: int | None) -> Any:
    target = await _plain(space)
    return await space.links.add(identity_id, "recovery", target, actor="ops", expected_revision=rev)


CASES: dict[str, Case] = {
    "update": Case(lambda s, i, r: s.identities.update(i, labels={"a": 1}, actor="ops", expected_revision=r)),
    "replace_profile": Case(
        lambda s, i, r: s.identities.replace_profile(i, {"country": "DE"}, actor="ops", expected_revision=r)
    ),
    "replace_attributes": Case(
        lambda s, i, r: s.identities.replace_attributes(
            i, {"device": "iphone"}, actor="ops", expected_revision=r
        )
    ),
    "replace_labels": Case(
        lambda s, i, r: s.identities.replace_labels(i, {"b": 2}, actor="ops", expected_revision=r)
    ),
    "add_tags": Case(lambda s, i, r: s.identities.add_tags(i, ["vip"], actor="ops", expected_revision=r)),
    "set_tags": Case(lambda s, i, r: s.identities.set_tags(i, ["new"], actor="ops", expected_revision=r)),
    "set_note": Case(lambda s, i, r: s.identities.set_note(i, "n", actor="ops", expected_revision=r)),
    "retire": Case(lambda s, i, r: s.identities.retire(i, actor="ops", reason="x", expected_revision=r)),
    "set_lifecycle": Case(
        lambda s, i, r: s.identities.set_lifecycle(i, "retired", actor="ops", reason="x", expected_revision=r)
    ),
    "override": Case(
        lambda s, i, r: s.identities.override(
            i,
            Override(region="standing", key="", state=Standing.LIMITED, reason="r", actor="admin"),
            expected_revision=r,
        )
    ),
    "ban": Case(lambda s, i, r: s.identities.ban(i, reason="r", actor="admin", expected_revision=r)),
    "freeze": Case(lambda s, i, r: s.identities.freeze(i, reason="r", actor="admin", expected_revision=r)),
    "cool_down": Case(
        lambda s, i, r: s.identities.cool_down(
            i, for_=timedelta(minutes=5), actor="worker", expected_revision=r
        )
    ),
    "observe": Case(
        lambda s, i, r: s.identities.observe(
            i,
            Observation("standing", "", Standing.LIMITED),
            actor="probe",
            source=ChangeSource.PROBE,
            expected_revision=r,
        )
    ),
    "restore_standing": Case(
        lambda s, i, r: s.identities.restore_standing(
            i, actor="ops", reason="апелляция", expected_revision=r
        ),
        prepare=_banned,
    ),
    "unpin": Case(
        lambda s, i, r: s.identities.unpin(
            i, "standing", "", actor="admin", reason="снять", expected_revision=r
        ),
        prepare=_pinned,
    ),
    "replace_credentials": Case(
        lambda s, i, r: s.secrets.replace_credentials(
            i, {"phone": "+2", "password": "n"}, actor="ops", expected_revision=r
        )
    ),
    "replace_session": Case(
        lambda s, i, r: s.secrets.replace_session(i, {"auth_key": "k2"}, actor="ops", expected_revision=r)
    ),
    "links.add": Case(_link_target),
    "links.remove": Case(
        lambda s, i, r: s.links.remove(i, "recovery", actor="ops", expected_revision=r), prepare=_linked
    ),
    "requirements.add": Case(
        lambda s, i, r: s.verifications.requirements.add(i, "selfie", actor="platform", expected_revision=r)
    ),
    "requirements.resolve": Case(
        lambda s, i, r: s.verifications.requirements.resolve(
            i, "selfie", actor="admin", reason="готово", expected_revision=r
        ),
        prepare=_with_requirement,
    ),
    "verifications.review": Case(
        lambda s, i, r: s.verifications.review(
            i, "kyc_id", "approve", actor="admin", reason="документы", expected_revision=r
        ),
        prepare=_under_review,
    ),
    "delete": Case(lambda s, i, r: s.identities.delete(i, actor="ops", expected_revision=r)),
}


async def _events(space: SpaceHandle, identity_id: UUID) -> int:
    page = await space.admin.event_page(
        filters=EventFilters(identity_id=identity_id, include_journal=True), limit=200
    )
    return len(page.items)


@pytest.mark.parametrize("name", sorted(CASES))
async def test_stale_revision_conflicts_and_current_applies(space: SpaceHandle, name: str) -> None:
    case = CASES[name]
    identity_id = await (case.prepare or _plain)(space)
    revision = (await space.identities.get(identity_id)).revision
    events = await _events(space, identity_id)

    with pytest.raises(RevisionConflict) as caught:
        await case.command(space, identity_id, revision - 1)

    assert (caught.value.expected_version, caught.value.actual_version) == (revision - 1, revision)
    assert (await space.identities.get(identity_id)).revision == revision
    assert await _events(space, identity_id) == events  # конфликт ничего не записал

    await case.command(space, identity_id, revision)
    if name != "delete":
        assert (await space.identities.get(identity_id)).revision == revision + 1
    else:
        assert await space.identities.find(identity_id) is None


async def test_none_keeps_unconditional_behaviour(space: SpaceHandle) -> None:
    identity_id = await _plain(space)
    await space.identities.add_tags(identity_id, ["a"], actor="ops")

    updated = await space.identities.add_tags(identity_id, ["b"], actor="ops", expected_revision=None)

    assert updated.revision == 3 and set(updated.tags) == {"a", "b"}


@pytest.mark.parametrize("bad", [-1, True, "2", 1.0])
async def test_invalid_expected_revision_is_rejected(space: SpaceHandle, bad: Any) -> None:
    identity_id = await _plain(space)

    with pytest.raises(ValidationFailed):
        await space.identities.update(identity_id, note="n", actor="ops", expected_revision=bad)


async def test_unchanged_command_still_checks_revision(space: SpaceHandle) -> None:
    identity_id = await _plain(space)
    await space.identities.add_tags(identity_id, ["a"], actor="ops")

    with pytest.raises(RevisionConflict):
        await space.identities.add_tags(identity_id, ["a"], actor="ops", expected_revision=1)
    same = await space.identities.add_tags(identity_id, ["a"], actor="ops", expected_revision=2)

    assert same.revision == 2
