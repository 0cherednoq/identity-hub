"""N2.2: именованные команды состояния и публичная точка входа наблюдений."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from identity_hub import (
    Availability,
    ChangeSource,
    IdentityCreate,
    Observation,
    Override,
    RevisionConflict,
    SpaceHandle,
    Standing,
    ValidationFailed,
)
from identity_hub.domain.regions import Observation as MachineObservation

pytestmark = [pytest.mark.conformance]


async def _new(space: SpaceHandle, ref: str) -> Any:
    return await space.identities.create(
        IdentityCreate(service="fake_tg", external_ref=ref, lifecycle="ready", session={"auth_key": "k"}),
        actor="test",
    )


@pytest.mark.parametrize(
    ("method", "target"),
    [("ban", Standing.BANNED), ("freeze", Standing.FROZEN)],
)
async def test_named_standing_command_matches_override(
    space: SpaceHandle, method: str, target: Standing
) -> None:
    named = await _new(space, f"named-{method}")
    direct = await _new(space, f"direct-{method}")

    named_decision = await getattr(space.identities, method)(named.id, reason="ручное решение", actor="admin")
    direct_decision = await space.identities.override(
        direct.id,
        Override("standing", "", target, reason="ручное решение", actor="admin"),
    )

    assert named_decision.outcome == direct_decision.outcome
    assert named_decision.events == direct_decision.events
    assert named_decision.state.state == direct_decision.state.state == target
    assert (await space.identities.get(named.id)).standing is target


@pytest.mark.parametrize("method", ["ban", "freeze"])
async def test_named_standing_command_does_not_revoke_live_lease(space: SpaceHandle, method: str) -> None:
    identity = await _new(space, f"live-{method}")
    lease = await space.pools.define(f"live-{method}", service="fake_tg", holder="worker").acquire()
    assert lease.identity_id == identity.id

    await getattr(space.identities, method)(identity.id, reason="ручное решение", actor="admin")

    assert not lease.lost
    await lease.renew()
    await lease.release()


async def test_cool_down_accepts_absolute_or_relative_deadline_and_only_extends(
    space: SpaceHandle,
) -> None:
    relative = await _new(space, "cool-relative")
    absolute = await _new(space, "cool-absolute")
    deadline = datetime(2035, 1, 1, tzinfo=UTC)

    first = await space.identities.cool_down(relative.id, for_=timedelta(minutes=5), actor="worker")
    second = await space.identities.cool_down(relative.id, for_=timedelta(minutes=1), actor="worker")
    exact = await space.identities.cool_down(absolute.id, until=deadline, actor="worker")

    assert first.state.state == second.state.state == exact.state.state == Availability.COOLING
    assert first.state.until is not None and first.state.until == second.state.until
    assert exact.state.until == deadline
    assert (await space.identities.get(relative.id)).availability is Availability.COOLING


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"until": datetime(2035, 1, 1, tzinfo=UTC), "for_": timedelta(seconds=1)},
        {"for_": timedelta(0)},
        {"until": datetime(2035, 1, 1)},
    ],
)
async def test_cool_down_rejects_ambiguous_or_invalid_deadline(
    space: SpaceHandle, kwargs: dict[str, Any]
) -> None:
    identity = await _new(space, f"invalid-{len(kwargs)}-{kwargs!r}")
    with pytest.raises(ValidationFailed):
        await space.identities.cool_down(identity.id, actor="worker", **kwargs)


async def test_public_observe_uses_the_same_machine_as_internal_observation(
    space: SpaceHandle,
) -> None:
    public = await _new(space, "observe-public")
    internal = await _new(space, "observe-internal")
    observed_at = datetime(2030, 1, 1, tzinfo=UTC)

    public_decision = await space.identities.observe(
        public.id,
        Observation("standing", "", Standing.LIMITED, observed_at, raw_status="LIMIT", code="rate"),
        actor="platform-adapter",
        source=ChangeSource.WEBHOOK,
    )
    internal_decision = await space._observe(
        internal.id,
        "standing",
        MachineObservation(
            Standing.LIMITED,
            observed_at,
            source=ChangeSource.WEBHOOK,
            raw_status="LIMIT",
            code="rate",
            actor="platform-adapter",
        ),
    )

    assert public_decision.outcome == internal_decision.outcome
    assert public_decision.events == internal_decision.events
    assert public_decision.state.state == internal_decision.state.state == Standing.LIMITED
    assert public_decision.state.change_source is ChangeSource.WEBHOOK
    assert public_decision.state.changed_by == "platform-adapter"


async def test_public_observe_validates_key_and_expected_revision(space: SpaceHandle) -> None:
    identity = await _new(space, "observe-guard")
    observation = Observation("standing", "typo", Standing.LIMITED)

    with pytest.raises(ValidationFailed):
        await space.identities.observe(identity.id, observation, actor="probe", source=ChangeSource.PROBE)
    with pytest.raises(RevisionConflict):
        await space.identities.observe(
            identity.id,
            Observation("standing", "", Standing.LIMITED),
            actor="probe",
            source=ChangeSource.PROBE,
            expected_revision=identity.revision - 1,
        )
