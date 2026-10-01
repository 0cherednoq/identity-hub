"""pytest-плагин ревью: какие методы Store/StoreTx реально вызываются на каждом бэкенде."""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

from identity_hub.postgres.store import PgStore, PgTx
from identity_hub.store.base import Store, StoreTx
from identity_hub.testing.memory import MemoryStore, MemoryTx

CALLS: dict[str, dict[str, set[str]]] = {"memory": {}, "pg": {}}
CURRENT = {"test": "?"}


def _wrap(cls: type, kind: str, base: type, prefix: str) -> None:
    for name in sorted(base.__abstractmethods__):
        original = cls.__dict__.get(name)
        if original is None:
            continue

        def make(orig, method_name):
            @functools.wraps(orig)
            def wrapper(*args, **kwargs):
                CALLS[kind].setdefault(f"{prefix}.{method_name}", set()).add(CURRENT["test"])
                return orig(*args, **kwargs)

            return wrapper

        setattr(cls, name, make(original, name))


_wrap(MemoryTx, "memory", StoreTx, "StoreTx")
_wrap(MemoryStore, "memory", Store, "Store")
_wrap(PgTx, "pg", StoreTx, "StoreTx")
_wrap(PgStore, "pg", Store, "Store")


def pytest_runtest_setup(item):
    CURRENT["test"] = item.nodeid.split("[")[0]


def pytest_sessionfinish(session, exitstatus):
    all_methods = sorted(
        [f"StoreTx.{n}" for n in StoreTx.__abstractmethods__] + [f"Store.{n}" for n in Store.__abstractmethods__]
    )
    out = {
        "all": all_methods,
        "memory": {k: sorted(v) for k, v in CALLS["memory"].items()},
        "pg": {k: sorted(v) for k, v in CALLS["pg"].items()},
    }
    target = Path(os.environ.get("IH_COV_OUT", "ih_cov.json"))
    target.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
