"""IMP-23 (I8.1): поверхность SpaceHandle, карта миграции и предупреждения об устаревании."""

from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest

from identity_hub import SpaceHandle
from identity_hub.api import Namespace, warn_deprecated
from identity_hub.api._migration import LEGACY, NAMESPACES, PERMANENT

ROOT = Path(__file__).parents[1]
MIGRATION_DOC = ROOT / "docs" / "api-migration.md"
PLANNED_NAMESPACES = {
    "admin",
    "analytics",
    "auth",
    "data",
    "identities",
    "imports",
    "links",
    "maintenance",
    "pools",
    "proxies",
    "secrets",
    "sync",
    "verifications",
}
DOC_ROW = re.compile(r"^\| `space\.(\w+)` \| `space\.([\w.]+)` \|", re.MULTILINE)


def _public(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_space_surface_allowlist() -> None:
    """Новое публичное имя появляется только как неймспейс, не как плоский метод."""
    assert _public(SpaceHandle) == PERMANENT | NAMESPACES | set(LEGACY)


def test_surface_groups_do_not_overlap() -> None:
    assert not PERMANENT & NAMESPACES
    assert not PERMANENT & set(LEGACY)
    assert not NAMESPACES & set(LEGACY)
    assert NAMESPACES <= PLANNED_NAMESPACES


def test_every_legacy_name_targets_planned_namespace() -> None:
    targets = {new.split(".", 1)[0] for new in LEGACY.values()}
    assert targets == PLANNED_NAMESPACES
    assert len(set(LEGACY.values())) == len(LEGACY), "две старые функции ведут в один метод"


def test_migration_doc_matches_legacy_map() -> None:
    rows = DOC_ROW.findall(MIGRATION_DOC.read_text(encoding="utf-8"))
    assert len(rows) == len(dict(rows)), "дубли в таблице миграции"
    assert dict(rows) == LEGACY


def test_warn_deprecated_points_to_caller_and_names_replacement() -> None:
    def alias() -> None:
        warn_deprecated("register")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        alias()

    assert len(caught) == 1
    warning = caught[0]
    assert warning.category is DeprecationWarning
    assert Path(warning.filename) == Path(__file__)
    assert "space.register" in str(warning.message)
    assert "space.identities.ensure" in str(warning.message)


def test_warn_deprecated_rejects_unknown_name() -> None:
    with pytest.raises(KeyError):
        warn_deprecated("no_such_method")


def test_namespace_holds_only_space_reference() -> None:
    class Probe(Namespace):
        __slots__ = ()

    space = object.__new__(SpaceHandle)
    space.slug = "prod"
    probe = Probe(space)
    assert repr(probe) == "Probe(space='prod')"
    with pytest.raises(AttributeError):
        probe.extra = 1  # type: ignore[attr-defined]
