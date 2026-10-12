"""Select conformance backends without starting unused infrastructure."""

from __future__ import annotations

import os

_KNOWN_BACKENDS = ("memory", "postgres")


def selected_backends() -> tuple[str, ...]:
    """Return backends requested through ``IH_TEST_BACKENDS``.

    The default preserves the full local suite. CI can select ``memory`` for its
    fast jobs and run ``postgres`` once in the dedicated integration job.
    """

    raw = os.environ.get("IH_TEST_BACKENDS", ",".join(_KNOWN_BACKENDS))
    selected = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = sorted(set(selected) - set(_KNOWN_BACKENDS))
    if not selected or unknown:
        choices = ", ".join(_KNOWN_BACKENDS)
        detail = f"unknown: {', '.join(unknown)}" if unknown else "no backend selected"
        raise ValueError(f"invalid IH_TEST_BACKENDS ({detail}); choose from: {choices}")
    return selected
