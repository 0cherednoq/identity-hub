import inspect
import pytest


@pytest.mark.asyncio
async def test_hub_fixture(identity_hub):
    assert not inspect.iscoroutine(identity_hub), "identity_hub — корутина, а не хаб"
    assert identity_hub.services
