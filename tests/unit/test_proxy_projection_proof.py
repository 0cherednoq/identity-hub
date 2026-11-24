"""PA5/R12: новая удовлетворённая ветка, а не соседняя техническая проекция."""

import pytest

from identity_hub import AllOf, AnyOf, RegionIn
from identity_hub.proxy_finalization import _positive_proof

pytestmark = pytest.mark.unit


def test_previous_preconditions_can_support_new_matching_fact():
    rule = AllOf(RegionIn("auth", states={"authenticated"}), RegionIn("standing", states={"good", "limited"}))
    facts = {("auth", "default"): "authenticated", ("standing", ""): "limited"}
    assert _positive_proof(rule, facts, {("standing", ""): "limited"})
    assert not _positive_proof(rule, facts, {("availability", ""): "cooling"})


def test_anyof_rejected_branch_cannot_launder_previous_matching_branch():
    rule = AnyOf(RegionIn("auth", states={"authenticated"}), RegionIn("availability", states={"available"}))
    facts = {("auth", "default"): "authenticated", ("availability", ""): "cooling"}
    assert not _positive_proof(rule, facts, {("availability", ""): "cooling"})
    assert _positive_proof(rule, facts, {("auth", "default"): "authenticated"})


def test_projected_value_must_still_match_complete_snapshot():
    rule = RegionIn("standing", states={"good", "limited"})
    assert not _positive_proof(rule, {("standing", ""): "limited"}, {("standing", ""): "good"})


def test_new_matching_fact_does_not_bypass_other_preconditions():
    rule = AllOf(RegionIn("auth", states={"authenticated"}), RegionIn("standing", states={"good"}))
    facts = {("auth", "default"): "authenticated", ("standing", ""): "banned"}
    assert not _positive_proof(rule, facts, {("auth", "default"): "authenticated"})
