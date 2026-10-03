"""IMP-23 (I8.1, I8.10): поверхность SpaceHandle — только неймспейсы; устаревших алиасов нет."""

from __future__ import annotations

import importlib.util

import pytest

from identity_hub import ProxyPolicy, SpaceHandle
from identity_hub.api import Namespace
from identity_hub.pool import Pool

NAMESPACES = {
    "admin",
    "analytics",
    "auth",
    "data",
    "identities",
    "imports",
    "links",
    "maintenance",
    "notifications",
    "pools",
    "proxies",
    "secrets",
    "sync",
    "verifications",
    "webhooks",
}
PERMANENT = {"ref", "using"}


def _public(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_space_surface_allowlist() -> None:
    """Новое публичное имя появляется только как неймспейс, не как плоский метод (F6.4)."""
    assert _public(SpaceHandle) == NAMESPACES | PERMANENT


def test_deprecated_forms_are_removed() -> None:
    """R1-09: алиасы до первой публикации удалены, а не доживают до 1.0.0b1."""
    assert not hasattr(Pool, "check_capacity")
    assert importlib.util.find_spec("identity_hub.api._migration") is None
    with pytest.raises(TypeError):
        ProxyPolicy(pool="res")  # type: ignore[call-arg]


def test_proxy_policy_needs_group() -> None:
    assert ProxyPolicy(group="res").group == "res"
    assert ProxyPolicy("res").group == "res"
    with pytest.raises(ValueError, match="group"):
        ProxyPolicy()


def test_namespace_holds_only_space_reference() -> None:
    class Probe(Namespace):
        __slots__ = ()

    space = object.__new__(SpaceHandle)
    space.slug = "prod"
    probe = Probe(space)
    assert repr(probe) == "Probe(space='prod')"
    with pytest.raises(AttributeError):
        probe.extra = 1  # type: ignore[attr-defined]
