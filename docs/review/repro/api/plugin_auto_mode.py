import inspect


def test_unrelated():
    assert 1 + 1 == 2


async def test_hub_fixture(identity_hub):
    # в strict-режиме pytest-asyncio фикстура не будет разрешена в объект хаба
    assert not inspect.iscoroutine(identity_hub), "identity_hub — корутина, а не хаб"
    assert identity_hub.services
