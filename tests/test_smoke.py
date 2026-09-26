import pytest

import identity_hub
from identity_hub.cli import main

pytestmark = pytest.mark.unit


def test_version() -> None:
    assert identity_hub.__version__


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert identity_hub.__version__ in capsys.readouterr().out
