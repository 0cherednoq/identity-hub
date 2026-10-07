"""Чистые свойства GCRA без хранилища и реального времени."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from identity_hub.domain.throttling import gcra_take
from identity_hub.service import Throttle


def test_gcra_random_sequence_respects_sliding_window() -> None:
    rng = random.Random(42)
    rule = Throttle(7, timedelta(seconds=60))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tat = None
    accepted: list[datetime] = []
    for _ in range(2_000):
        now += timedelta(milliseconds=rng.randrange(0, 4_000))
        delay, candidate = gcra_take(now, tat, rule)
        if not delay:
            tat = candidate
            accepted.append(now)
    for point in accepted:
        assert sum(point <= item < point + rule.per for item in accepted) <= rule.limit


def test_burst_adds_exactly_burst_minus_one_capacity() -> None:
    rule = Throttle(4, timedelta(seconds=20), burst=3)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    tat = None
    accepted = 0
    for _ in range(10):
        delay, candidate = gcra_take(now, tat, rule)
        if not delay:
            tat, accepted = candidate, accepted + 1
    assert accepted == rule.burst
