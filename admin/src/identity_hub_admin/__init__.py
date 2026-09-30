"""Web administration application for Identity Hub."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("identity-hub-admin")
except PackageNotFoundError:  # pragma: no cover - import directly from a source checkout
    __version__ = "0.1.0.dev0"

__all__ = ["__version__"]
