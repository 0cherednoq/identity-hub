"""P7.1: контекст аренды в логах (ТЗ FR-12.1)."""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from identity_hub.context import LeaseContext, _current
from identity_hub.logging import LeaseContextFilter, structlog_processor

pytestmark = pytest.mark.unit


def _ctx() -> LeaseContext:
    return LeaseContext(uuid4(), uuid4(), "fake_tg", "parsers", "host:1")


def test_logging_filter_adds_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.lease")
    logger.addFilter(LeaseContextFilter())
    ctx = _ctx()
    token = _current.set(ctx)
    try:
        with caplog.at_level(logging.INFO, logger="test.lease"):
            logger.info("inside")
    finally:
        _current.reset(token)
    with caplog.at_level(logging.INFO, logger="test.lease"):
        logger.info("outside")
    inside, outside = caplog.records
    assert inside.lease_id == str(ctx.lease_id)  # type: ignore[attr-defined]
    assert inside.pool == "parsers"  # type: ignore[attr-defined]
    assert outside.lease_id is None  # type: ignore[attr-defined]


def test_structlog_processor() -> None:
    ctx = _ctx()
    token = _current.set(ctx)
    try:
        event = structlog_processor(None, "info", {"event": "x", "pool": "explicit"})
    finally:
        _current.reset(token)
    assert event["lease_id"] == str(ctx.lease_id)
    assert event["pool"] == "explicit"  # явное значение не перетирается
    assert structlog_processor(None, "info", {"event": "y"}) == {"event": "y"}
