"""Миксины SpaceHandle не должны перекрывать методы друг друга (молчаливая подмена по MRO)."""

from __future__ import annotations

from collections import defaultdict

import pytest

from identity_hub._space import SpaceBase
from identity_hub.hub import SpaceHandle

pytestmark = pytest.mark.unit


def test_no_method_name_collisions() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    base_names = set(vars(SpaceBase))
    for cls in SpaceHandle.__mro__[1:]:
        if cls in (SpaceBase, object):
            continue
        for name in vars(cls):
            if name.startswith("__") or name in base_names:
                continue
            owners[name].append(cls.__name__)
    clashes = {name: classes for name, classes in owners.items() if len(classes) > 1}
    assert clashes == {}
