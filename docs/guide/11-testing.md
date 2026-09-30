# 11. Тестирование приложения

Для тестов PostgreSQL не нужен: `MemoryStore` реализует то же хранилище в памяти, а
pytest-плагин даёт готовые фикстуры.

```bash
uv add --dev "identity-hub[testing]"
```

Плагин подключается автоматически. Фикстура хаба асинхронная, поэтому включите
`asyncio_mode = "auto"` для pytest-asyncio:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

## Фикстуры

| Фикстура | Что даёт |
|---|---|
| `identity_hub` | Запущенный `IdentityHub` на `MemoryStore`, `strict_events=True` |
| `identity_hub_space` | Пространство `test` |
| `identity_hub_services` | Сервисы хаба — **переопределите**, чтобы подставить свои |
| `identity_hub_service` | Тестовый сервис `test_service` с операцией `work` |
| `identity_hub_make_identity` | Фабрика аккаунтов: по умолчанию `ready` и `authenticated` |
| `identity_hub_clock` | `FakeClock`; `.advance(timedelta)` двигает время |
| `identity_hub_store`, `identity_hub_secrets` | Хранилище и ключи |

## Пример

```python
# conftest.py
import pytest
from myapp.services import TELEGRAM   # send_message: Quota(20, timedelta(hours=1))

@pytest.fixture
def identity_hub_services():
    return (TELEGRAM,)


# test_sender.py
from datetime import timedelta
from identity_hub import Override, QuotaExhausted
import pytest

# фабрика по умолчанию делает аккаунт authenticated с пустой сессией —
# для сервиса со своей схемой сессии передайте валидную сессию
SESSION = {"auth_key": "test"}

async def test_quota_stops_sending(identity_hub_space, identity_hub_make_identity):
    await identity_hub_make_identity(service="telegram", tags=["sender"], session=SESSION)
    pool = identity_hub_space.pool("senders", service="telegram", tags=["sender"])

    for i in range(20):
        async with pool.lease() as lease:
            async with lease.operation("send_message", key=f"t:{i}"):
                pass

    with pytest.raises(QuotaExhausted):
        async with pool.lease() as lease:
            async with lease.operation("send_message", key="t:21"):
                pass


async def test_cooldown_expires(identity_hub_space, identity_hub_make_identity, identity_hub_clock):
    identity_id = await identity_hub_make_identity(service="telegram", session=SESSION)
    pool = identity_hub_space.pool("p", service="telegram")
    async with pool.lease() as lease:
        async with lease.operation("send_message") as op:
            await op.cool_down(timedelta(minutes=5))

    identity_hub_clock.advance(timedelta(minutes=6))
    async with pool.lease() as lease:               # снова доступен
        assert lease.identity_id == identity_id
```

Проверка событий: подпишитесь, выполните действие, затем `await identity_hub.dispatcher.drain()`.

```python
async def test_ban_event(identity_hub, identity_hub_space, identity_hub_make_identity):
    seen = []
    async def collect(event):
        seen.append(event)
    identity_hub.events.subscribe("t", collect, types={"identity.standing.changed"})

    identity_id = await identity_hub_make_identity(service="telegram", session=SESSION)
    await identity_hub_space.override(identity_id, Override("standing", "", "banned", reason="t", actor="t"))
    await identity_hub.dispatcher.drain()
    assert seen[-1].data["to"] == "banned"
```

Классификаторы ошибок, пробы и `Authenticator` тестируйте так же: подставьте фейковый клиент
платформы, бросающий нужные исключения, и проверьте состояние через `space.identity_card(id)`.

## Ограничения MemoryStore

- Нет `space.using(session)` — транзакции приложения работают только с PostgreSQL.
- Нет конкурентности между процессами; поведение `SKIP LOCKED` и гонок проверяйте на PostgreSQL.
- Для интеграционных тестов с PostgreSQL используйте testcontainers или отдельную базу и
  `IdentityHub(sessionmaker=...)`.

## Свой адаптер хранилища

Если вы пишете свою реализацию `Store`, прогоните общий набор проверок:

```python
from identity_hub.testing.conformance import StoreConformanceSuite

class TestMyStore(StoreConformanceSuite):
    @pytest.fixture
    async def store_under_test(self):
        yield MyStore(...)
```

Дальше: [рецепты](12-recipes.md).
