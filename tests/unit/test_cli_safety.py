"""F3.11: живучесть демонов, ошибки фабрики без текста, подтверждение, `doctor renormalize`."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest

from identity_hub import IdentityCreate, IdentityHub, StaticSecretsProvider
from identity_hub.cli import main
from identity_hub.clock import SystemClock
from identity_hub.testing import MemoryStore, make_service

pytestmark = pytest.mark.unit

ME = __name__
SECRET = "hunter2"
_CREATED: list[IdentityHub] = []
_SLEEPS: list[float] = []
_CALLS: list[str] = []


class _Stop(BaseException):
    """Останавливает бесконечный цикл демона в тесте."""


class _RecordingClock(SystemClock):
    async def sleep(self, seconds: float) -> None:
        _SLEEPS.append(seconds)


@pytest.fixture(autouse=True)
def _reset() -> None:
    _CREATED.clear()
    _SLEEPS.clear()
    _CALLS.clear()


def _new_hub(**options: Any) -> IdentityHub:
    hub = IdentityHub(
        store=MemoryStore(),
        secrets=StaticSecretsProvider({"k": bytes(range(32))}, "k"),
        clock=_RecordingClock(),
        **options,
    )
    _CREATED.append(hub)
    return hub


def plain_factory() -> IdentityHub:
    hub = _new_hub()

    async def record(name: str, *_args: Any, **_kwargs: Any) -> int:
        _CALLS.append(name)
        return 0

    hub._rollups.purge_minutes = lambda **kw: record("purge_minutes", **kw)  # type: ignore[method-assign]
    hub._rollups.rebuild = lambda *a, **kw: record("rebuild", *a, **kw)  # type: ignore[method-assign]
    hub._state_snapshots.purge = lambda **kw: record("snapshots_purge", **kw)  # type: ignore[method-assign]
    return hub


# ---------------------------------------------------------------- подтверждение

REBUILD = ["--rebuild", "--space", "main", "--start", "2026-01-01T00:00:00Z", "--end", "2026-01-02T00:00:00Z"]


@pytest.mark.parametrize(
    "argv,call",
    [
        (["rollups", "--purge-minutes"], "purge_minutes"),
        (["rollups", *REBUILD], "rebuild"),
        (["snapshots", "--purge"], "snapshots_purge"),
    ],
)
def test_destructive_commands_require_yes(
    argv: list[str], call: str, capsys: pytest.CaptureFixture[str]
) -> None:
    command = [argv[0], "--factory", f"{ME}:plain_factory", *argv[1:]]

    with pytest.raises(SystemExit) as exc:
        main(command)

    assert exc.value.code == 2
    assert "--yes" in capsys.readouterr().err
    assert not _CREATED  # отказ до создания хаба: к базе команда не подключалась

    assert main([*command, "--yes"]) == 0
    assert [call] == _CALLS


def test_non_destructive_commands_do_not_ask(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["snapshots", "--factory", f"{ME}:plain_factory", "--backfill-current"]) == 0
    assert main(["maintenance", "--factory", f"{ME}:plain_factory", "--once"]) == 0


# ---------------------------------------------------------------- R2-S18: ошибки фабрики и команд


def failing_factory() -> IdentityHub:
    raise ValueError(f"postgresql://app:{SECRET}@db/app: неверный адрес")


def wrong_type_factory() -> object:
    return object()


def broken_doctor_factory() -> IdentityHub:
    hub = _new_hub()

    async def check(**_kwargs: Any) -> list[Any]:
        raise TypeError(f"password={SECRET}")

    hub.doctor.check = check  # type: ignore[method-assign]
    return hub


@pytest.mark.parametrize(
    "factory,expected",
    [("failing_factory", "ValueError"), ("wrong_type_factory", "IdentityHub")],
)
def test_factory_failure_is_exit_1_without_exception_text(
    factory: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["doctor", "--factory", f"{ME}:{factory}"]) == 1

    err = capsys.readouterr().err
    assert "error[factory_failed]" in err and expected in err
    assert SECRET not in err and "usage:" not in err


def test_internal_error_is_not_reported_as_bad_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    """Внутренний `TypeError` — код 1 и имя типа, а не «ошибка аргументов» с текстом исключения."""
    assert main(["doctor", "--factory", f"{ME}:broken_doctor_factory"]) == 1

    err = capsys.readouterr().err
    assert "error[internal]: TypeError" in err
    assert SECRET not in err and "usage:" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor", "--factory", "no-colon"],
        ["doctor", "--factory", "identity_hub_missing_module:factory"],
        ["doctor", "--factory", f"{ME}:missing_factory"],
        ["rotate-key", "--factory", f"{ME}:plain_factory", "--batch-size", "0"],
        ["rollups", "--factory", f"{ME}:plain_factory", "--rebuild", "--yes"],
        ["rollups", "--factory", f"{ME}:plain_factory", *REBUILD[:-1], "2026-01-02T00:00:00", "--yes"],
        ["snapshots", "--factory", f"{ME}:plain_factory", "--purge", "--before", "вчера", "--yes"],
    ],
)
def test_argument_mistakes_are_exit_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_password_in_url_argument_is_warned_about(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from identity_hub.postgres import migrate as migrate_module

    seen: list[str] = []

    async def fake_migrate(engine: Any, **_kwargs: Any) -> None:
        seen.append(engine.url.render_as_string(hide_password=True))

    monkeypatch.setattr(migrate_module, "migrate", fake_migrate)
    url = f"postgresql+asyncpg://app:{SECRET}@127.0.0.1:1/app"

    assert main(["migrate", "--url", url]) == 0
    err = capsys.readouterr().err
    assert "IDENTITY_HUB_DATABASE_URL" in err and SECRET not in err

    monkeypatch.setenv("IDENTITY_HUB_DATABASE_URL", url)
    assert main(["migrate"]) == 0  # из окружения — без предупреждения
    assert capsys.readouterr().err == ""
    assert len(seen) == 2


# ---------------------------------------------------------------- R2-F16: демоны


def flaky_maintenance_factory() -> IdentityHub:
    hub = _new_hub()
    outcomes: list[Any] = [ConnectionError(f"db down {SECRET}"), ConnectionError("still down"), 0, _Stop()]

    async def run_due() -> int:
        _CALLS.append("run_due")
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return int(outcome)

    hub.maintenance.run_due = run_due  # type: ignore[method-assign]
    return hub


def test_maintenance_daemon_survives_failed_ticks(caplog: pytest.LogCaptureFixture) -> None:
    """Сбой БД на тике не завершает процесс: лог без текста исключения, растущая пауза."""
    with caplog.at_level(logging.ERROR, logger="identity_hub"), pytest.raises(_Stop):
        main(["maintenance", "--factory", f"{ME}:flaky_maintenance_factory"])

    assert _CALLS == ["run_due"] * 4
    assert _SLEEPS == [1.0, 2.0, 1.0]  # backoff после сбоев, затем обычный интервал
    assert len(caplog.records) == 2
    assert "ConnectionError" in caplog.text and SECRET not in caplog.text


def flaky_dispatcher_factory() -> IdentityHub:
    hub = _new_hub()
    outcomes: list[BaseException] = [ConnectionError(f"listen failed {SECRET}"), OSError("reset"), _Stop()]

    async def run(*_args: Any, **_kwargs: Any) -> None:
        _CALLS.append("run")
        raise outcomes.pop(0)

    hub.dispatcher.run = run  # type: ignore[method-assign]
    return hub


def test_dispatcher_daemon_restarts_after_failure(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="identity_hub"), pytest.raises(_Stop):
        main(["dispatcher", "--factory", f"{ME}:flaky_dispatcher_factory"])

    assert _CALLS == ["run"] * 3
    assert _SLEEPS == [1.0, 2.0]
    assert SECRET not in caplog.text


# ---------------------------------------------------------------- doctor renormalize

_REFS: list[str] = []


async def renormalize_factory() -> IdentityHub:
    mail = make_service("mail")
    hub = _new_hub(services=[mail])
    await hub.start()
    space = hub.space("main", "Main", create=True)
    for ref in _REFS:
        await space.identities.create(IdentityCreate(service="mail", external_ref=ref))
    # объявление сервиса сменилось в коде: новая версия нормализации
    hub._services["mail"] = replace(
        mail, normalize_ref=lambda ref: ref.strip().lower(), normalize_ref_version=1
    )
    return hub


def test_doctor_renormalize_updates_refs(capsys: pytest.CaptureFixture[str]) -> None:
    _REFS[:] = ["User@Mail.com", "Other@Mail.com"]
    argv = ["doctor", "--factory", f"{ME}:renormalize_factory", "renormalize", "--service", "mail"]

    assert main(argv) == 0

    assert capsys.readouterr().out.splitlines() == ["updated: 2"]


def test_doctor_renormalize_reports_collisions_with_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    _REFS[:] = ["A@X.COM", "a@x.com"]
    argv = ["doctor", "renormalize", "--factory", f"{ME}:renormalize_factory", "--service", "mail"]

    assert main(argv) == 1

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "updated: 0"
    assert len(lines) == 2 and lines[1].startswith("collision: ")
    assert "x.com" not in "\n".join(lines).lower()  # значения ref в вывод не попадают


def test_doctor_renormalize_unknown_service_is_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    _REFS[:] = []
    argv = ["doctor", "--factory", f"{ME}:renormalize_factory", "renormalize", "--service", "nope"]

    assert main(argv) == 2
    assert "error[" in capsys.readouterr().err
