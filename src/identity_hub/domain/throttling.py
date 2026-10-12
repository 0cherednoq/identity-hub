"""Чистая GCRA-машина для ограничения частоты запросов."""

from __future__ import annotations

from datetime import datetime, timedelta

from identity_hub.service import Throttle


def gcra_take(now: datetime, tat: datetime | None, throttle: Throttle) -> tuple[timedelta, datetime]:
    """Вернуть ожидание и новый TAT; при ненулевом ожидании слот не занят."""
    interval = throttle.interval
    current = tat or now
    allowed_at = current - interval * (throttle.burst - 1)
    if now < allowed_at:
        return allowed_at - now, current
    return timedelta(0), max(current, now) + interval
