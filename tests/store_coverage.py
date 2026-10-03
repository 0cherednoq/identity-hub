"""F5.3b (R2-P23): каждый метод Store и StoreTx вызывается тестами на обоих бэкендах.

Плагин включается переменной `IH_STORE_COVERAGE=1` (CI, job `check` на Python 3.14) и имеет смысл
только при полном прогоне: в конце сессии он перечисляет методы контракта без вызова на
`MemoryStore` или PostgreSQL и делает прогон неуспешным.
"""

from __future__ import annotations

import functools
from typing import Any

import pytest

from identity_hub.postgres.store import PgStore, PgTx
from identity_hub.store.base import Store, StoreTx
from identity_hub.testing.memory import MemoryStore, MemoryTx

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
    missing = {
        backend: [name for name in required() if name not in called] for backend, called in _CALLED.items()
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
