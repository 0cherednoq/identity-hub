"""Статический контракт диагностического допуска (AR2.1)."""

from __future__ import annotations

from typing import assert_type

from identity_hub import Availability, DiagnosticPolicy, Lifecycle, Service, Standing
from identity_hub.errors import Blocker


def _diagnostics_contract(service: Service) -> None:
    policy = DiagnosticPolicy(availability=frozenset(Availability))
    assert_type(policy.lifecycles, frozenset[Lifecycle])
    assert_type(policy.availability, frozenset[Availability])
    assert_type(policy.standing, frozenset[Standing])
    assert_type(policy.blockers(Lifecycle.READY, Availability.PAUSED, Standing.GOOD), tuple[Blocker, ...])
    assert_type(service.diagnostic_policy(), DiagnosticPolicy)
