"""PA0.2: декларативные решения о назначении не зависят от checker и хранилища."""

from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from identity_hub import AllOf, AnyOf, ProxyBindingPolicy, ProxyPolicy, RegionIn, Service, ValidationFailed
from identity_hub.proxy_assignment import compose_proxy_policy, predicate_from_dict
from identity_hub.service import LeaseMode

pytestmark = pytest.mark.unit


def test_predicates_are_immutable_and_missing_facts_fail_closed() -> None:
    states = {"authenticated"}
    auth = RegionIn("auth", states=states)
    states.add("no_session")
    assert auth.states == frozenset({"authenticated"})
    assert auth.key == "default"
    rule = AllOf(auth, AnyOf(RegionIn("standing", states={"good"}), RegionIn("standing", states={"limited"})))
    assert rule.matches({("auth", "default"): "authenticated", ("standing", ""): "limited"})
    assert not rule.matches({("standing", ""): "good"})
    with pytest.raises(FrozenInstanceError):
        auth.key = "other"  # type: ignore[misc]


def test_predicate_roundtrip_is_canonical_and_rejects_unknown_fields() -> None:
    rule = AnyOf(RegionIn("standing", states={"limited", "good"}), RegionIn("lifecycle", states={"ready"}))
    assert predicate_from_dict(rule.to_dict()) == rule
    assert rule.to_dict()["rules"][0]["states"] == ["good", "limited"]
    with pytest.raises(ValueError):
        predicate_from_dict({"type": "region", "region": "standing", "states": ["good"], "callback": "x"})
    with pytest.raises(ValueError):
        predicate_from_dict({"type": "python", "expression": "x"})


@pytest.mark.parametrize(
    ("region", "key", "states"),
    [
        ("unknown", "", {"good"}),
        ("standing", "default", {"good"}),
        ("auth", "", {"unknown"}),
        ("verification", "", {"verified"}),
        ("standing", "", set()),
    ],
)
def test_invalid_region_rule_is_rejected(region: str, key: str, states: set[str]) -> None:
    with pytest.raises(ValueError):
        RegionIn(region, key=key, states=states)


def test_composite_must_be_nonempty_and_cannot_contain_callbacks() -> None:
    with pytest.raises(ValueError):
        AllOf()
    with pytest.raises(ValueError):
        AnyOf(lambda: True)  # type: ignore[arg-type]


def test_predicate_size_is_bounded_before_recursive_deserialization() -> None:
    rule = RegionIn("standing", states={"good"})
    with pytest.raises(ValueError):
        AllOf(*(rule for _ in range(64)))
    value: dict[str, Any] = rule.to_dict()
    for _ in range(8):
        value = {"type": "all", "rules": [value]}
    with pytest.raises(ValueError):
        predicate_from_dict(value)
    with pytest.raises(ValueError):
        predicate_from_dict({"type": "all", "rules": [rule.to_dict()] * 64})


def test_binding_policy_roundtrip_preserves_independent_rules() -> None:
    policy = ProxyBindingPolicy(
        bind_when=RegionIn("auth", states={"authenticated"}),
        unbind_when=RegionIn("standing", states={"banned"}),
    )
    assert ProxyBindingPolicy.from_dict(policy.to_dict()) == policy
    with pytest.raises(ValueError):
        ProxyBindingPolicy.from_dict({**policy.to_dict(), "callback": "x"})


def test_proxy_policy_defaults_preserve_legacy_and_deferred_requires_sticky() -> None:
    assert ProxyPolicy(group="g").binding is None
    with pytest.raises(ValueError):
        ProxyPolicy(group="g", sticky=False, binding=ProxyBindingPolicy())
    with pytest.raises(ValueError):
        ProxyBindingPolicy(mode="immediate", bind_when=RegionIn("standing", states={"good"}))


def test_service_validates_declared_factors_slots_and_exclusive_mode() -> None:
    for rule in (
        RegionIn("verification", key="unknown", states={"verified"}),
        RegionIn("auth", key="unknown", states={"authenticated"}),
    ):
        with pytest.raises(ValidationFailed):
            Service(
                name="site",
                title="Site",
                proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy(bind_when=rule)),
            )
    service = Service(
        name="site", title="Site", proxy_policy=ProxyPolicy(group="g", binding=ProxyBindingPolicy())
    )
    with pytest.raises(ValidationFailed):
        replace(service, lease_mode=LeaseMode.SHARED)


def test_pool_override_cannot_weaken_service_rules() -> None:
    base = ProxyPolicy(
        group="a",
        binding=ProxyBindingPolicy(
            bind_when=RegionIn("auth", states={"authenticated"}),
            unbind_when=RegionIn("standing", states={"banned"}),
        ),
    )
    assert compose_proxy_policy(base, None) == base
    inherited = compose_proxy_policy(base, ProxyPolicy(group="b"))
    assert inherited is not None and inherited.group == "b" and inherited.binding == base.binding
    stricter = compose_proxy_policy(
        base,
        ProxyPolicy(
            group="b",
            binding=ProxyBindingPolicy(
                bind_when=RegionIn("standing", states={"good"}),
                unbind_when=RegionIn("lifecycle", states={"retired"}),
            ),
        ),
    )
    assert stricter is not None and stricter.binding is not None
    assert stricter.binding.bind_when is not None
    assert not stricter.binding.bind_when.matches({("auth", "default"): "authenticated"})
    assert stricter.binding.unbind_when is not None
    assert stricter.binding.unbind_when.matches({("standing", ""): "banned"})
    with pytest.raises(ValueError):
        compose_proxy_policy(base, ProxyPolicy(group="b", sticky=False))
    with pytest.raises(ValueError):
        compose_proxy_policy(base, ProxyPolicy(group="b", binding=ProxyBindingPolicy(mode="immediate")))
