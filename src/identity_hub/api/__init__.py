"""Ресурсные неймспейсы пространства (IMP-23, D-141).

`space.identities`, `space.imports`, `space.proxies` и т.д. — тонкие фасады над логикой
`SpaceHandle`. Плоских методов у `SpaceHandle` нет: только неймспейсы, `using` и `ref`.
"""

from identity_hub.api._base import Namespace

__all__ = ["Namespace"]
