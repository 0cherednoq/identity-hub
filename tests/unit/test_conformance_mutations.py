"""F5.3a (R2-P04): `StoreConformanceSuite` ловит каждый намеренный дефект Store."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.broken_stores import MUTATIONS

pytest_plugins = ("pytester",)
pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[2]
SUITE = """
import pytest

from identity_hub.testing.conformance import StoreConformanceSuite
from tests.fixtures.broken_stores import make_broken


class TestBrokenStore(StoreConformanceSuite):
    @pytest.fixture
    async def store_under_test(self):
        yield make_broken({mutation!r})
"""


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
def test_suite_fails_on_broken_store(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    pytester.makeini("[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(SUITE.format(mutation=mutation))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-q")

    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) + outcomes.get("errors", 0) >= 1, (
        f"набор не заметил дефект «{MUTATIONS[mutation]}»"
    )


def test_suite_passes_on_reference_memory_store(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль: без дефекта тот же запуск проходит — падения выше вызваны именно дефектами."""
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    pytester.makeini("[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(
        """
import pytest

from identity_hub.testing import MemoryStore
from identity_hub.testing.conformance import StoreConformanceSuite


class TestReference(StoreConformanceSuite):
    @pytest.fixture
    async def store_under_test(self):
        yield MemoryStore()
"""
    )

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-q")

    outcomes = result.parseoutcomes()
    assert outcomes.get("failed", 0) == 0 and outcomes.get("passed", 0) >= 10
