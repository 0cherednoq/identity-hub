"""Ресурсные неймспейсы пространства (IMP-23, D-141).

`space.identities`, `space.imports`, `space.proxies` и т.д. — тонкие фасады над логикой
`SpaceHandle`. Старые плоские методы `SpaceHandle` остаются алиасами с `DeprecationWarning`
до `1.0.0b1`; соответствие имён — `identity_hub.api._migration.LEGACY`.
"""

from identity_hub.api._base import Namespace, warn_deprecated

__all__ = ["Namespace", "warn_deprecated"]
