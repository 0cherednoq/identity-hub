# Changelog

Значимые пользовательские изменения Identity Hub фиксируются в этом файле.
Формат основан на Keep a Changelog; до первого стабильного релиза API может
меняться без периода совместимости.

## [Unreleased]

### Added

- `SecretValue`: секрет со скрытым `repr`/`str`, без JSON и pickle; scrubber событий его отвергает.
- Ресурсные неймспейсы пространства (`space.identities`, `space.imports`, `space.secrets`,
  `space.proxies`, `space.pools`, `space.auth`, `space.sync`, `space.links`, `space.data`,
  `space.verifications`, `space.admin`, `space.analytics`, `space.maintenance`) с единой
  конвенцией глаголов; соответствие старых имён — [`docs/api-migration.md`](docs/api-migration.md).
- `LoginOutcome` (`StrEnum`, значения прежних строк), `LoginReport`, `ReconcileReport` в корне пакета.
- `hub.read`: публичная read-схема на SQLAlchemy Core (`identities`, `leases`, `lease_history`,
  `region_states`, `events`, `proxies`) без секретов для join с таблицами приложения и `among=`;
  версия `READ_SCHEMA_VERSION`.
- Руководство на новом API и страница [«Карта API»](docs/guide/api.md): точка входа, неймспейсы,
  типовые операции по сущностям, изменение состояний и лимиты.
- `Identity`, `IdentityCreate`, `IdentityFilter`, `Page`, `CreateManyResult`, `SecretRef`, `Proxy`.
- Стабильные коды ошибок `IdentityHubError.code` и безопасный `context`; функции `http_status()`
  и `exit_code()`; новые типы `RevisionConflict`, `IdempotencyConflict`, `LeaseOwnershipLost`,
  `LeaseRevoked`, `LeaseExpired`, `DatabaseBusy`, `SchemaIncompatible`,
  `SecretConfigurationError`. CLI печатает `error[<code>]` и возвращает код выхода по таблице.

- `Operation.previous_outcome` — исход прошлой попытки с тем же `key`
  (`"succeeded"`, `"failed"` или `None`).

### Changed

- Ключ операции `lease.operation(kind, key=...)` действует в пределах пространства, а не всей
  базы (миграция `0009`). Повтор после неуспешной попытки — новая попытка со списанием квоты
  (`duplicate=False`, `previous_outcome="failed"`); после успешной — `duplicate=True`. Тот же
  ключ с другим видом операции — `IdempotencyConflict`. Запись ключа переживает удаление
  аккаунта и хранится `Retention.operation_keys` с момента последней попытки.
- `idempotency_key` аренды действует в пределах пространства и принадлежит одной команде: пулу,
  сервису и `holder`. Повтор от другого пула или `holder` — `IdempotencyConflict` (миграция `0010`).
- Сбой записи результата после успешно выполненной операции не выходит к вызывающему:
  предупреждение в лог и метрика `identity_hub_failures_total{scope="operation_bookkeeping"}`.
- Контракт `Store`: `charge_quotas(..., idempotency=IdempotencyRequest)`,
  `claim_idempotency_key`, `complete_idempotency_key` вместо `operation_key`/`finish_operation_key`.
- Уникальность identity — `(service, normalized_ref)`: `Service.normalize_ref` и
  `normalize_ref_version`, миграция `0008`, `hub.doctor.renormalize()` для смены нормализатора.
- `hub.start()` проверяет ревизию схемы и бросает `SchemaIncompatible`, если она не та, что
  ожидает код; `identity-hub migrate` берёт advisory lock на схему.
- Associated data шифрования в формате `identity-hub:v1:…`; записи `1.0.0a1` читаются, `rotate-key`
  переводит их в новый формат. Лимиты: JSON до 64 KiB и 16 уровней, секрет до 512 KiB.
- Единый порядок блокировок ([`docs/lock-order.md`](docs/lock-order.md)) и
  `StoreTx.lock_identities()`; проверка границ импортов `lint-imports` в `scripts/check.sh`.
- `Pool.lease()` и `Pool.acquire()` принимают явные параметры: опечатка в имени — `TypeError`.
- `DecryptionFailed` наследует `SecretConfigurationError` (код `decryption_failed`).

### Fixed

- Повтор операции с тем же `key` после сетевой ошибки больше не выглядит как «уже выполнено»:
  раньше `op.duplicate` был `True` и действие не повторялось никогда.
- `pool.lease(idempotency_key=...)` из пула другого сервиса больше не возвращает чужую аренду
  вместе с её credentials; тот же ключ в другом пространстве не даёт `IntegrityError`.
- Deadlock двух параллельных импортов с `on_existing="update_session"` и общими ref.

### Deprecated

- Плоские методы `SpaceHandle` (`register`, `search`, `identity_card`, `import_many`,
  `reveal_secret`, `add_proxy`, `pool` и другие) — алиасы с `DeprecationWarning` до `1.0.0b1`.
- `ProxyPolicy(pool=...)` → `ProxyPolicy(group=...)`; `Pool.check_capacity()` → `Pool.capacity()`.

## [1.0.0a1] - 2026-09-30

Первый публичный альфа-релиз. API может меняться между альфа-версиями.

### Added

- Полное руководство по установке, быстрому старту, CLI и production-интеграции.
- Исполняемый Telegram-подобный пример на `MemoryStore`.
- Руководство `docs/guide`: сервисы, аккаунты, машины состояний, вход, верификация,
  аренды, лимиты, прокси, события, тестирование, рецепты и ограничения альфа-версии;
  диаграммы архитектуры и состояний; исполняемый сценарий `examples/account_lifecycle.py`.

### Fixed

- `LeaseMode.SHARED`: параллельные shared-аренды одного аккаунта больше не получают
  `StaleFencing` — shared-захват не увеличивает fencing; в PostgreSQL shared-аккаунты
  выдаются по давности последнего захвата, а не всегда один и тот же; `idempotency_key`
  для shared-пула даёт `ValidationFailed` вместо молчаливого игнорирования (D-140).
- `identity-hub rollups --purge-minutes` ничего не делал из-за недостижимого кода.
- Стабильная корневая поверхность импортов для сценария публичного API.
- Матрица трассировки всех требований FR/G/T/S на исполняемые тесты.
- Публичные read-модели (overview, аренды, прокси, события, доставки, очередь
  ручных действий), аналитические rollups и почасовые снимки состояния;
  миграции `0005`–`0007`.

### Changed

- Extra `telegram` больше не тянет `aiogram`: уведомления отправляются прямым
  вызовом Bot API `sendMessage` через `httpx` (`HttpTelegramTransport`).
  Протокол `TelegramTransport` не изменился.
- Ответы Bot API 400/401/403/404 (`TelegramRejected`) сразу переводят уведомление
  в `dead` без исчерпания ретраев; сеть и 5xx (`TelegramUnavailable`) повторяются.
- Sdist содержит только библиотеку, тесты, примеры и пользовательскую документацию;
  веб-админка в пакет не входит.

## [0.1.0.dev0] - 2026-09-27

### Added

- Декларативные сервисы, пространства, аккаунты, регионы состояний и связи.
- Конкурентно безопасные аренды с heartbeat, fencing, квотами и pacing.
- Зашифрованные credentials, сессии, прокси и управляемая ротация KEK.
- PostgreSQL-хранилище, встроенные миграции Alembic и отдельные схемы.
- Прокси, вход, challenge, верификации, пробы, импорт и полнотекстовый поиск.
- Outbox, dispatcher, вебхуки, Telegram-уведомления, maintenance и метрики.
- CLI `migrate`, `dispatcher`, `maintenance`, `doctor` и `rotate-key`.
- `MemoryStore`, pytest-плагин, фабрики и общий conformance-набор для адаптеров.
