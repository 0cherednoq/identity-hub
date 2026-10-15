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
        (["doctor", "--factory", "app:hub", "renormalize", "--service", "mail"], "doctor"),
        (["doctor", "--factory", "app:hub", "rekey-fingerprints"], "doctor"),
        (["rotate-key", "--factory", "app:hub", "--batch-size", "10"], "rotate-key"),
        (
            [
                "secrets",
                "--factory",
                "app:hub",
                "convert",
                "--to",
                "plaintext",
                "--batch",
                "10",
            ],
            "secrets",
        ),
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

    # F3.11: удаление данных требует подтверждения
    code = main(
        ["rollups", "--factory", f"{__name__}:memory_hub_factory", "--purge-minutes", "--yes", *extra]
    )

    assert code == 0
    assert [expected] == _PURGE_CALLS
    assert capsys.readouterr().out.strip() == "7"


_K1, _K2 = b"\x01" * 32, b"\x02" * 32


async def _rotation_hub(keys: dict[str, bytes]) -> IdentityHub:
    """Хаб с одним прокси под `k1`; затем процесс «перезапущен» с набором `keys` и активным `k2`."""
    from identity_hub.crypto import Envelope

    hub = IdentityHub(store=MemoryStore(), secrets=StaticSecretsProvider({"k1": _K1}, "k1"))
    await hub.start()
    await hub.space("main", "Main", create=True).proxies.create("http://user:pw@10.0.0.1:8080", group="g")
    hub._envelope = Envelope(StaticSecretsProvider(keys, "k2"))
    return hub


async def rotation_hub_factory() -> IdentityHub:
    return await _rotation_hub({"k1": _K1, "k2": _K2})


async def lost_key_hub_factory() -> IdentityHub:
    return await _rotation_hub({"k2": _K2})


def test_rotate_key_reports_completion(capsys: pytest.CaptureFixture[str]) -> None:
    """F1.8: всё переобёрнуто — число записей и код 0."""
    assert main(["rotate-key", "--factory", f"{__name__}:rotation_hub_factory"]) == 0
    assert capsys.readouterr().out.split() == ["1"]


def test_rotate_key_exits_nonzero_when_rows_stay_under_old_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R2-S08: остаток под прежним ключом и нечитаемые строки — в выводе и в коде возврата."""
    assert main(["rotate-key", "--factory", f"{__name__}:lost_key_hub_factory"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "0"
    assert "remaining: proxies k1 1" in lines
    assert any(line.startswith("unreadable: proxies ") for line in lines)
    assert not any("pw" in line.split() for line in lines)


def test_doctor_reports_rows_under_inactive_key(capsys: pytest.CaptureFixture[str]) -> None:
    for extra in ([], ["--fix"]):  # --fix ключи не меняет: это работа rotate-key
        assert main(["doctor", "--factory", f"{__name__}:rotation_hub_factory", *extra]) == 1
        assert "issue: inactive-key proxies k1 1" in capsys.readouterr().out.splitlines()


async def conversion_hub_factory(*, plaintext: bool = False) -> IdentityHub:
    from identity_hub import IdentityCreate
    from tests.fixtures.services import FAKE_TG

    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": _K1}, "k"),
        services=[FAKE_TG],
        secret_storage="plaintext" if plaintext else "encrypted",
    )
    await hub.start()
    await hub.space("main", create=True).identities.create(
        IdentityCreate(
            service="fake_tg",
            external_ref="+cli-convert",
            credentials={"phone": "+cli-convert", "password": "p"},
            session={"auth_key": "s"},
        ),
        actor="test",
    )
    return hub


async def plaintext_hub_factory() -> IdentityHub:
    return await conversion_hub_factory(plaintext=True)


def test_secrets_convert_and_doctor_warning(capsys: pytest.CaptureFixture[str]) -> None:
    factory = f"{__name__}:conversion_hub_factory"
    assert main(["secrets", "--factory", factory, "convert", "--to", "plaintext", "--batch", "1"]) == 0
    assert capsys.readouterr().out.strip() == "2"

    plaintext_factory = f"{__name__}:plaintext_hub_factory"
    assert main(["doctor", "--factory", plaintext_factory]) == 0
    assert "warning: credentials и сессии хранятся открытыми: 2" in capsys.readouterr().out


def test_keygen_prints_usable_key(capsys: pytest.CaptureFixture[str]) -> None:
    """C6.2: `keygen` печатает ключ в формате `IDENTITY_HUB_KEK` и ничего лишнего."""
    from identity_hub.crypto import parse_keys

    assert main(["keygen", "--key-id", "k7"]) == 0
    out = capsys.readouterr()
    line = out.out.strip()
    active, keys = parse_keys(line)
    assert active == "k7" and len(keys["k7"]) == 32 and out.err == ""
    assert main(["keygen"]) == 0
    assert capsys.readouterr().out.startswith("k1:")


def test_missing_secrets_error_tells_how_to_create_a_key() -> None:
    """C6.2: ошибка без мастер-ключа называет переменную окружения и команду создания ключа."""
    from identity_hub import ValidationFailed

    with pytest.raises(ValidationFailed) as error:
        IdentityHub(store=MemoryStore())
    message = str(error.value)
    assert "identity-hub keygen" in message and "IDENTITY_HUB_KEK" in message


def test_migrate_prefers_dedicated_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """C6.2: отдельный адрес миграций (в обход PgBouncer) важнее общего адреса базы."""
    from identity_hub import cli

    monkeypatch.setenv("IDENTITY_HUB_DATABASE_URL", "postgresql+asyncpg://bouncer/app")
    monkeypatch.setenv("IDENTITY_HUB_MIGRATE_DATABASE_URL", "postgresql+asyncpg://direct/app")
    args = _parser().parse_args(["migrate"])
    assert cli._migrate_url(args) == "postgresql+asyncpg://direct/app"
    assert cli._migrate_url(_parser().parse_args(["migrate", "--url", "postgresql+asyncpg://x/y"])) == (
        "postgresql+asyncpg://x/y"
    )
    monkeypatch.delenv("IDENTITY_HUB_MIGRATE_DATABASE_URL")
    assert cli._migrate_url(args) == "postgresql+asyncpg://bouncer/app"
    monkeypatch.delenv("IDENTITY_HUB_DATABASE_URL")
    with pytest.raises(SystemExit) as stopped:  # адрес не задан — ошибка аргументов
        main(["migrate"])
    assert stopped.value.code == 2
