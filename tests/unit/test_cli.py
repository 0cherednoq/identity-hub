"""P9.2: поверхность argparse CLI."""

from __future__ import annotations

import pytest

from identity_hub.cli import _parser, main

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "argv,command",
    [
        (["migrate", "--url", "postgresql+asyncpg://localhost/db"], "migrate"),
        (["dispatcher", "--factory", "app:hub", "--once"], "dispatcher"),
        (["maintenance", "--factory", "app:hub", "--once"], "maintenance"),
        (["doctor", "--factory", "app:hub", "--fix"], "doctor"),
        (["rotate-key", "--factory", "app:hub", "--batch-size", "10"], "rotate-key"),
        (
            [
                "rollups",
                "--factory",
                "app:hub",
                "--rebuild",
                "--space",
                "main",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-02T00:00:00Z",
            ],
            "rollups",
        ),
        (["rollups", "--factory", "app:hub", "--purge-minutes"], "rollups"),
        (["snapshots", "--factory", "app:hub", "--backfill-current"], "snapshots"),
        (
            [
                "snapshots",
                "--factory",
                "app:hub",
                "--purge",
                "--before",
                "2026-01-01T00:00:00Z",
            ],
            "snapshots",
        ),
    ],
)
def test_commands_parse(argv: list[str], command: str) -> None:
    assert _parser().parse_args(argv).command == command


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip()
