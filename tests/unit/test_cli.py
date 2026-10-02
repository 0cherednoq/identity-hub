"""P9.2: поверхность argparse CLI."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from identity_hub.cli import _parser, main
from identity_hub.crypto import StaticSecretsProvider
from identity_hub.hub import IdentityHub
from identity_hub.testing import MemoryStore

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


_PURGE_CALLS: list[datetime | None] = []


def memory_hub_factory() -> IdentityHub:
    """Фабрика для CLI-тестов: MemoryStore и запись вызовов purge_minutes."""
    hub = IdentityHub(store=MemoryStore(), secrets=StaticSecretsProvider({"k": b"\x01" * 32}, "k"))

    async def purge_minutes(*, before: datetime | None = None) -> int:
        _PURGE_CALLS.append(before)
        return 7

    hub._rollups.purge_minutes = purge_minutes  # type: ignore[method-assign]
    return hub


@pytest.mark.parametrize(
    "extra,expected",
    [
        ([], None),
        (["--before", "2026-01-01T00:00:00+00:00"], datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_rollups_purge_minutes_runs(
    extra: list[str], expected: datetime | None, capsys: pytest.CaptureFixture[str]
) -> None:
    _PURGE_CALLS.clear()

    code = main(["rollups", "--factory", f"{__name__}:memory_hub_factory", "--purge-minutes", *extra])

    assert code == 0
    assert [expected] == _PURGE_CALLS
    assert capsys.readouterr().out.strip() == "7"
