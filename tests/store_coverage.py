"""F5.3b (R2-P23): каждый метод Store и StoreTx вызывается тестами на выбранных бэкендах.

Плагин включается переменной `IH_STORE_COVERAGE=1` и имеет смысл только при полном прогоне
выбранного через `IH_TEST_BACKENDS` бэкенда. В конце сессии он перечисляет методы контракта без
вызова и делает прогон неуспешным.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from identity_hub.postgres.store import PgStore, PgTx
from identity_hub.store.base import Store, StoreTx
from identity_hub.testing.memory import MemoryStore, MemoryTx
from tests._backends import selected_backends

_CALLED: dict[str, set[str]] = {"memory": set(), "postgres": set()}


def _wrap(implementation: type, backend: str, contract: type, prefix: str) -> None:
    for name in sorted(contract.__abstractmethods__):
        original = implementation.__dict__.get(name)
        if original is None:
            continue

        def make(function: Any, method: str) -> Any:
            @functools.wraps(function)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                _CALLED[backend].add(f"{prefix}.{method}")
                return function(*args, **kwargs)

            return wrapper

        setattr(implementation, name, make(original, name))


def required() -> list[str]:
    return sorted(
        [f"Store.{name}" for name in Store.__abstractmethods__]
        + [f"StoreTx.{name}" for name in StoreTx.__abstractmethods__]
    )


def pytest_configure(config: pytest.Config) -> None:
    _wrap(MemoryTx, "memory", StoreTx, "StoreTx")
    _wrap(MemoryStore, "memory", Store, "Store")
    _wrap(PgTx, "postgres", StoreTx, "StoreTx")
    _wrap(PgStore, "postgres", Store, "Store")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    coverage_file = os.environ.get("IH_STORE_COVERAGE_FILE")
    if coverage_file:
        path = Path(coverage_file)
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            previous = {}
        for backend, names in previous.items():
            if backend in _CALLED:
                _CALLED[backend].update(names)
        path.write_text(
            json.dumps({backend: sorted(names) for backend, names in _CALLED.items()}),
            encoding="utf-8",
        )
        if os.environ.get("IH_STORE_COVERAGE_FINAL") != "1":
            return
    missing = {
        backend: [name for name in required() if name not in _CALLED[backend]]
        for backend in selected_backends()
    }
    lines = [f"{backend}: {', '.join(names)}" for backend, names in missing.items() if names]
    if not lines:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("методы Store без вызова в тестах (F5.3b):", red=True)
        for line in lines:
            reporter.write_line(f"  {line}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
