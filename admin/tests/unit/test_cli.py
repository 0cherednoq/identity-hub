from __future__ import annotations

import pytest

from identity_hub_admin.cli import main


def test_check_accepts_development_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IH_ADMIN_ENVIRONMENT", "development")

    assert main(["check"]) == 0
    assert "configuration OK" in capsys.readouterr().out


def test_check_rejects_incomplete_production(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IH_ADMIN_ENVIRONMENT", "production")
    monkeypatch.delenv("IH_ADMIN_DATABASE_URL", raising=False)
    monkeypatch.delenv("IH_ADMIN_HUB_FACTORY", raising=False)
    monkeypatch.delenv("IH_ADMIN_KEK_KEYS", raising=False)

    assert main(["check"]) == 1
    assert "ERROR:" in capsys.readouterr().out


def test_demo_command_refuses_production(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IH_ADMIN_ENVIRONMENT", "production")

    assert main(["demo"]) == 1
    assert "IH_ADMIN_DEMO_MODE is forbidden in production" in capsys.readouterr().out


def test_hash_password_is_interactive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(("strong password", "strong password"))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

    assert main(["hash-password"]) == 0
    assert capsys.readouterr().out.startswith("$argon2id$")
