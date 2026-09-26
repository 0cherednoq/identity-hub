"""P9.3: независимый MetricsHook."""

from __future__ import annotations

import pytest

from identity_hub.metrics import RecordingMetrics, record_metric

pytestmark = pytest.mark.unit


def test_recording_metrics_and_backend_failure_isolated() -> None:
    metrics = RecordingMetrics()
    record_metric(metrics, "counter", "failures", reason="quota")
    record_metric(metrics, "gauge", "queue", 3, queue="webhooks")
    assert [(event.kind, event.name, event.value) for event in metrics.events] == [
        ("counter", "failures", 1.0),
        ("gauge", "queue", 3.0),
    ]

    class Broken:
        def counter(self, *_: object, **__: object) -> None:
            raise RuntimeError("metrics backend down")

    record_metric(Broken(), "counter", "ignored")  # type: ignore[arg-type]
