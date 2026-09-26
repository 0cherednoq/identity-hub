"""Командная строка identity-hub. Команды добавляются в P9.2."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="identity-hub")
    parser.add_argument("--version", action="store_true", help="показать версию")
    args = parser.parse_args(argv)
    if args.version:
        from identity_hub import __version__

        print(__version__)
    return 0
