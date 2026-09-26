"""Измерение latency и компактная статистика benchmark-сценариев."""

from __future__ import annotations

import math
import platform
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["Latency", "environment", "measure", "summarize"]


@dataclass(frozen=True, slots=True)
class Latency:
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    maximum_ms: float
    throughput: float


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize(samples: Sequence[float], elapsed: float | None = None) -> Latency:
    """Секундные отсчёты → p50/p95/p99 в миллисекундах и throughput."""

    if not samples:
        raise ValueError("нужен хотя бы один отсчёт")
    total = elapsed if elapsed is not None else sum(samples)
    return Latency(
        count=len(samples),
        p50_ms=_percentile(samples, 0.50) * 1000,
        p95_ms=_percentile(samples, 0.95) * 1000,
        p99_ms=_percentile(samples, 0.99) * 1000,
        maximum_ms=max(samples) * 1000,
        throughput=len(samples) / total if total > 0 else math.inf,
    )


async def measure(operation: Callable[[], Awaitable[Any]], *, iterations: int, warmup: int = 1) -> Latency:
    for _ in range(warmup):
        await operation()
    samples: list[float] = []
    total_started = time.perf_counter()
    for _ in range(iterations):
        started = time.perf_counter()
        await operation()
        samples.append(time.perf_counter() - started)
    return summarize(samples, time.perf_counter() - total_started)


def environment() -> dict[str, str | int]:
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu": platform.processor() or "unknown",
        "logical_cpu": __import__("os").cpu_count() or 0,
        "timer_resolution_ns": int(statistics.mean([time.get_clock_info("perf_counter").resolution]) * 1e9),
    }
