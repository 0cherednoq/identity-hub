"""P1.2: машины регионов по диаграммам ТЗ §3 и правилам FR-9."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from identity_hub.domain.region_specs import (
    AUTH,
    AVAILABILITY,
    DELIVERY,
    LIFECYCLE,
    PROXY_HEALTH,
    STANDING,
)
from identity_hub.domain.regions import (
    Command,
    Observation,
    Outcome,
    Override,
    RegionSpec,
    RegionState,
    Timer,
    apply,
)
from identity_hub.enums import (
    AuthState,
    Availability,
    ChangeSource,
    Lifecycle,
    ProxyStatus,
    Severity,
    Standing,
)
from identity_hub.errors import TransitionNotAllowed, ValidationFailed

pytestmark = pytest.mark.unit

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ALL_SPECS = [LIFECYCLE, STANDING, AUTH, AVAILABILITY, PROXY_HEALTH, DELIVERY]


def at(state: str, **kw: object) -> RegionState:
    kw.setdefault("changed_at", T0)
    return RegionState(state=state, **kw)  # type: ignore[arg-type]


def sec(n: float) -> datetime:
    return T0 + timedelta(seconds=n)


# ---------------------------------------------------------------- таблица команд


def _pairs(spec: RegionSpec) -> list[tuple[RegionSpec, str, str, bool]]:
    return [
        (spec, src, dst, spec.can_command(src, dst))
        for src in sorted(spec.states)
        for dst in sorted(spec.states)
        if src != dst
    ]


@pytest.mark.parametrize(
    ("spec", "src", "dst", "allowed"),
    [p for spec in ALL_SPECS for p in _pairs(spec)],
    ids=lambda v: v.name if isinstance(v, RegionSpec) else str(v),
)
def test_command_table(spec: RegionSpec, src: str, dst: str, allowed: bool) -> None:
    cmd = Command(dst, actor="worker", reason="причина")
    if allowed:
        decision = apply(spec, at(src), cmd, sec(1))
        assert decision.outcome is Outcome.APPLIED
        assert decision.state.state == dst
        assert decision.state.change_source is ChangeSource.COMMAND
        assert decision.state.version == 2
    else:
        with pytest.raises(TransitionNotAllowed):
            apply(spec, at(src), cmd, sec(1))


@pytest.mark.parametrize(
    ("spec", "src", "dst"),
    [
        (LIFECYCLE, Lifecycle.PROVISIONING, Lifecycle.READY),
        (LIFECYCLE, Lifecycle.READY, Lifecycle.RETIRED),
        (LIFECYCLE, Lifecycle.RETIRED, Lifecycle.READY),
        (STANDING, Standing.GOOD, Standing.LIMITED),
        (STANDING, Standing.FROZEN, Standing.APPEALING),
        (STANDING, Standing.APPEALING, Standing.GOOD),
        (STANDING, Standing.APPEALING, Standing.FROZEN),
        (STANDING, Standing.FROZEN, Standing.DELETED),
        (STANDING, Standing.BANNED, Standing.GOOD),
        (AUTH, AuthState.LOGGING_IN, AuthState.CHALLENGE),
        (AUTH, AuthState.CHALLENGE, AuthState.LOGGING_IN),
        (AUTH, AuthState.AUTHENTICATED, AuthState.SESSION_INVALID),
        (AUTH, AuthState.SESSION_INVALID, AuthState.LOGGING_IN),
        (AVAILABILITY, Availability.AVAILABLE, Availability.PAUSED),
        (AVAILABILITY, Availability.AVAILABLE, Availability.COOLING),
        (AVAILABILITY, Availability.QUARANTINED, Availability.AVAILABLE),
        (PROXY_HEALTH, ProxyStatus.QUARANTINED, ProxyStatus.DEAD),
        (PROXY_HEALTH, ProxyStatus.DEAD, ProxyStatus.ALIVE),
    ],
)
def test_diagram_arrows_are_commands(spec: RegionSpec, src: str, dst: str) -> None:
    assert spec.can_command(src, dst)


@pytest.mark.parametrize(
    ("spec", "src", "dst"),
    [(LIFECYCLE, Lifecycle.RETIRED, Lifecycle.READY), (STANDING, Standing.BANNED, Standing.GOOD)],
)
def test_reason_required(spec: RegionSpec, src: str, dst: str) -> None:
    with pytest.raises(TransitionNotAllowed, match="причина"):
        apply(spec, at(src), Command(dst, actor="admin"), sec(1))
    assert apply(spec, at(src), Command(dst, actor="admin", reason="ошибка"), sec(1)).transitioned


def test_repeated_command_is_idempotent_and_until_only_extends() -> None:
    cur = at(Availability.COOLING, until=sec(100))
    same = apply(AVAILABILITY, cur, Command(Availability.COOLING, actor="w", until=sec(50)), sec(1))
    assert same.outcome is Outcome.UNCHANGED
    assert not same.changed
    longer = apply(AVAILABILITY, cur, Command(Availability.COOLING, actor="w", until=sec(200)), sec(1))
    assert longer.state.until == sec(200)
    assert longer.events == ()


def test_command_event_carries_severity_and_data() -> None:
    decision = apply(STANDING, at(Standing.GOOD), Command(Standing.BANNED, actor="w", code="spam"), sec(1))
    (event,) = decision.events
    assert event.type == "identity.standing.changed"
    assert event.severity is Severity.CRITICAL
    assert event.data["from"] == "good"
    assert event.data["to"] == "banned"
    assert event.data["code"] == "spam"


def test_unknown_target_state_rejected() -> None:
    with pytest.raises(ValidationFailed):
        apply(STANDING, at(Standing.GOOD), Command("weird", actor="w"), sec(1))


# ---------------------------------------------------------------- наблюдения


def test_observation_applies_from_any_state() -> None:
    obs = Observation(Standing.LIMITED, observed_at=sec(5), raw_status="SPAM", code="spam_restricted")
    decision = apply(STANDING, at(Standing.APPEALING), obs, sec(6))
    assert decision.outcome is Outcome.APPLIED
    assert decision.state.state == Standing.LIMITED
    assert decision.state.code == "spam_restricted"
    assert decision.state.raw_status == "SPAM"
    assert decision.state.synced_at == sec(6)
    assert decision.state.changed_at == sec(5)  # D-103
    assert decision.events[0].severity is Severity.WARNING
    assert decision.events[0].data["raw_status"] == "SPAM"


def test_stale_observation_dropped() -> None:
    cur = at(Standing.LIMITED, changed_at=sec(10))
    decision = apply(STANDING, cur, Observation(Standing.GOOD, observed_at=sec(9)), sec(11))
    assert decision.outcome is Outcome.STALE
    assert not decision.changed


def test_observation_between_probe_and_processing_is_not_stale() -> None:
    first = apply(STANDING, at(Standing.GOOD), Observation(Standing.LIMITED, observed_at=sec(10)), sec(20))
    second = apply(STANDING, first.state, Observation(Standing.GOOD, observed_at=sec(15)), sec(21))
    assert second.outcome is Outcome.APPLIED


def test_unknown_status_keeps_state_and_warns() -> None:
    decision = apply(
        STANDING, at(Standing.GOOD), Observation(None, observed_at=sec(1), raw_status="X"), sec(2)
    )
    assert decision.outcome is Outcome.UNKNOWN_STATUS
    assert decision.state.state == Standing.GOOD
    assert decision.state.raw_status == "X"
    assert decision.warning and "X" in decision.warning
    assert decision.events == ()


def test_confirming_observation_updates_sync_metadata_only() -> None:
    decision = apply(AUTH, at(AuthState.AUTHENTICATED), Observation(AuthState.AUTHENTICATED, sec(1)), sec(2))
    assert decision.outcome is Outcome.UNCHANGED
    assert decision.changed
    assert decision.state.synced_at == sec(2)
    assert decision.events == ()


def test_session_invalid_does_not_touch_standing() -> None:
    decision = apply(
        AUTH, at(AuthState.AUTHENTICATED), Observation(AuthState.SESSION_INVALID, sec(1)), sec(2)
    )
    assert decision.state.state == AuthState.SESSION_INVALID
    assert all(e.type == "identity.auth.changed" for e in decision.events)


def test_not_synced_region_rejects_observations() -> None:
    with pytest.raises(ValidationFailed):
        apply(LIFECYCLE, at(Lifecycle.READY), Observation(Lifecycle.RETIRED, sec(1)), sec(2))


def test_normalizer_applies() -> None:
    spec = RegionSpec(
        name="x",
        states=frozenset({"a", "b", "c"}),
        initial="a",
        transitions={},
        event_type="x.changed",
        normalize=lambda cur, obs: "c" if (cur, obs) == ("b", "a") else obs,
    )
    assert apply(spec, at("b"), Observation("a", sec(1)), sec(2)).state.state == "c"


# ---------------------------------------------------------------- confirm_leaving и sticky


def test_leaving_heavy_state_requires_two_consecutive_observations() -> None:
    cur = at(Standing.FROZEN)
    first = apply(STANDING, cur, Observation(Standing.GOOD, sec(1)), sec(1))
    assert first.outcome is Outcome.AWAITING_CONFIRMATION
    assert first.state.state == Standing.FROZEN
    assert first.state.pending_observation == Standing.GOOD
    second = apply(STANDING, first.state, Observation(Standing.GOOD, sec(2)), sec(2))
    assert second.outcome is Outcome.APPLIED
    assert second.state.state == Standing.GOOD
    assert second.state.pending_observation is None


def test_confirmation_resets_when_interrupted() -> None:
    first = apply(STANDING, at(Standing.FROZEN), Observation(Standing.GOOD, sec(1)), sec(1))
    back = apply(STANDING, first.state, Observation(Standing.FROZEN, sec(2)), sec(2))
    assert back.state.pending_observation is None
    again = apply(STANDING, back.state, Observation(Standing.GOOD, sec(3)), sec(3))
    assert again.outcome is Outcome.AWAITING_CONFIRMATION


def test_entering_heavy_state_is_immediate() -> None:
    decision = apply(STANDING, at(Standing.GOOD), Observation(Standing.FROZEN, sec(1)), sec(1))
    assert decision.state.state == Standing.FROZEN


def test_proxy_leaves_dead_only_after_confirmation() -> None:
    first = apply(PROXY_HEALTH, at(ProxyStatus.DEAD), Observation(ProxyStatus.ALIVE, sec(1)), sec(1))
    assert first.state.state == ProxyStatus.DEAD
    second = apply(PROXY_HEALTH, first.state, Observation(ProxyStatus.ALIVE, sec(2)), sec(2))
    assert second.state.state == ProxyStatus.ALIVE


def test_banned_is_sticky_for_observations() -> None:
    cur = at(Standing.BANNED)
    for n in (1, 2, 3):
        decision = apply(STANDING, cur, Observation(Standing.GOOD, sec(n)), sec(n))
        assert decision.outcome is Outcome.STICKY
        assert decision.events[0].type == "identity.state.drift"
        cur = decision.state
    assert cur.state == Standing.BANNED


def test_banned_to_deleted_by_observation_after_confirmation() -> None:
    first = apply(STANDING, at(Standing.BANNED), Observation(Standing.DELETED, sec(1)), sec(1))
    second = apply(STANDING, first.state, Observation(Standing.DELETED, sec(2)), sec(2))
    assert second.state.state == Standing.DELETED


# ---------------------------------------------------------------- override и закрепление


def test_override_requires_reason_and_actor() -> None:
    with pytest.raises(ValidationFailed):
        apply(STANDING, at(Standing.BANNED), Override(Standing.GOOD, reason=" ", actor="evgeny"), sec(1))
    with pytest.raises(ValidationFailed):
        apply(STANDING, at(Standing.BANNED), Override(Standing.GOOD, reason="ложный", actor=""), sec(1))


def test_override_with_pin_then_drift() -> None:
    ov = Override(Standing.GOOD, reason="бан ложный", actor="evgeny", pin_for=timedelta(hours=6))
    decision = apply(STANDING, at(Standing.BANNED), ov, sec(10))
    assert decision.state.state == Standing.GOOD
    assert decision.state.change_source is ChangeSource.OVERRIDE
    assert decision.state.pinned_until == sec(10) + timedelta(hours=6)
    assert [e.type for e in decision.events] == ["identity.standing.changed", "identity.state.overridden"]

    obs = apply(STANDING, decision.state, Observation(Standing.BANNED, sec(20), raw_status="BANNED"), sec(20))
    assert obs.outcome is Outcome.PINNED
    assert obs.state.state == Standing.GOOD
    assert obs.state.synced_at == sec(20)
    assert obs.state.raw_status == "BANNED"
    assert [e.type for e in obs.events] == ["identity.state.drift"]

    later = sec(10) + timedelta(hours=7)
    after = apply(STANDING, obs.state, Observation(Standing.BANNED, later), later)
    assert after.state.state == Standing.BANNED


def test_override_same_state_records_event() -> None:
    decision = apply(
        STANDING, at(Standing.GOOD), Override(Standing.GOOD, reason="проверено", actor="a"), sec(1)
    )
    assert [e.type for e in decision.events] == ["identity.state.overridden"]


# ---------------------------------------------------------------- таймер


def test_timer_expires_temporary_states() -> None:
    cur = at(Availability.COOLING, until=sec(60))
    assert apply(AVAILABILITY, cur, Timer(), sec(59)).outcome is Outcome.NOT_DUE
    decision = apply(AVAILABILITY, cur, Timer(), sec(60))
    assert decision.state.state == Availability.AVAILABLE
    assert decision.state.change_source is ChangeSource.TIMER
    assert decision.state.until is None
    assert apply(STANDING, at(Standing.LIMITED, until=sec(5)), Timer(), sec(6)).state.state == Standing.GOOD
    assert apply(STANDING, at(Standing.BANNED, until=sec(5)), Timer(), sec(6)).outcome is Outcome.NOT_DUE


# ---------------------------------------------------------------- инварианты


def _change_strategy(spec: RegionSpec) -> st.SearchStrategy[object]:
    states = sorted(spec.states)
    offsets = st.integers(min_value=-30, max_value=30)
    return st.one_of(
        st.builds(lambda s: Command(s, actor="w", reason="r"), st.sampled_from(states)),
        st.builds(
            lambda s, o: Observation(s, observed_at=o),
            st.one_of(st.none(), st.sampled_from(states)),
            offsets,
        ),
        st.builds(
            lambda s, pin: Override(
                s, reason="r", actor="a", pin_for=timedelta(seconds=pin) if pin else None
            ),
            st.sampled_from(states),
            st.integers(min_value=0, max_value=20),
        ),
        st.just(Timer()),
    )


@settings(max_examples=150, deadline=None)
@given(
    spec=st.sampled_from([STANDING, AUTH, PROXY_HEALTH]),
    data=st.data(),
)
def test_invariants_under_random_changes(spec: RegionSpec, data: st.DataObject) -> None:
    cur = spec.initial_state(T0)
    now = T0
    for _ in range(25):
        now += timedelta(seconds=data.draw(st.integers(min_value=0, max_value=10)))
        change = data.draw(_change_strategy(spec))
        if isinstance(change, Observation):
            change = Observation(change.state, observed_at=now + timedelta(seconds=change.observed_at))  # type: ignore[operator]
        try:
            decision = apply(spec, cur, change, now)  # type: ignore[arg-type]
        except TransitionNotAllowed:
            continue
        new = decision.state
        assert new.state in spec.states
        assert new.version >= cur.version
        assert new.changed_at >= cur.changed_at
        if decision.transitioned:
            assert decision.events
            assert new.pending_observation is None
        if new.change_source is ChangeSource.OVERRIDE:
            assert new.change_reason
        if cur.state in spec.sticky and isinstance(change, Observation):
            assert new.state == cur.state or new.state in spec.sticky_exits
        cur = new
