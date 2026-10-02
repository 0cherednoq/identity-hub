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

- `IdentityHub(redact_refs_in_events=True)`: `external_ref` и `labels` не попадают в события и
  вебхуки. `LeaseLogPolicy.max_field_chars`, `max_entry_bytes`, `buffer_ttl`.
- `Clock.sleep()` и `Clock.idle()`: ожидание аренды, heartbeat и опрос диспетчера идут по часам
  хаба. Под `FakeClock` `wait=` истекает, а `advance()` будит heartbeat активных аренд.
- `hub.doctor.inactive_keys()` и поля `RotationReport.remaining`, `unreadable`,
  `unreadable_total`, `complete`.
- `FileSecretsProvider.reload()` — перечитать файл мастер-ключей без перезапуска процесса.
- Событие `proxy.secret.revealed` (аудит `space.proxies.url`).
- `WebhookPolicy.proxy` — явный исходящий прокси доставки вебхуков; ошибка доставки
  `ForbiddenTarget`, если получатель резолвится в приватный или служебный адрес.
- `StorageError` (код `storage_error`): ошибка хранилища без SQL и параметров запроса;
  `DatabaseBusy.sqlstate`.
- `Operation.previous_outcome` — исход прошлой попытки с тем же `key`
  (`"succeeded"`, `"failed"` или `None`).

### Changed

- Ключ операции `lease.operation(kind, key=...)` действует в пределах пространства, а не всей
  базы (миграция `0009`). Повтор после неуспешной попытки — новая попытка со списанием квоты
  (`duplicate=False`, `previous_outcome="failed"`); после успешной — `duplicate=True`. Тот же
  ключ с другим видом операции — `IdempotencyConflict`. Запись ключа переживает удаление
  аккаунта и хранится `Retention.operation_keys` с момента последней попытки.
- В `last_error` истории аренды и в `data.error` событий `lease.*` сохраняется только имя типа
  исключения, а не его текст. `raw_status`, `code`, `hint` и причины команд обрезаются и очищаются
  от URL с учётными данными, query-строк и `Bearer …`.
- `scrub` событий сравнивает главное слово имени поля (`tokens_used`, `password_changed_at` больше
  не вырезаются; `api_key`, `authorization`, `jwt`, `otp`, `passphrase`, `proxy` — вырезаются),
  проверяет строки в списках и заменяет объект неизвестного типа его именем. Те же правила и лимит
  размера действуют для полей `extra` в `ih_lease_logs`.
- `MemoryStore()` без `clock` принимает часы хаба, хаб без `clock` — часы хранилища; разные
  часы — `ValidationFailed`. Свой `Clock` должен реализовать `sleep()` и `idle()`.
- `space.identities.delete()` при живой аренде бросает `ValidationFailed`; `revoke=True` отзывает
  аренду и закрывает её историю в той же транзакции.
- `lease.expired` от обслуживания несёт одинаковые поля на обоих хранилищах: `pool`, `holder`,
  `fencing`, `expired_at`.
- `lease.release(reason=...)` принимает только значения `LeaseEndReason`; другая строка —
  `ValidationFailed` до остановки heartbeat. После `release()` `lease.check()` бросает `LeaseLost`,
  даже если запись об освобождении не прошла.
- `proxy.pool.exhausted` пишется один раз на вызов `acquire` и не чаще раза в минуту на пул в
  процессе.
- `hub.doctor.rotate_key()` проходит все наборы до конца за один вызов (`limit` — размер пачки)
  и переоборачивает незавершённые фоновые импорты. CLI `rotate-key` печатает остаток и
  нечитаемые строки и возвращает 1, если под прежним ключом что-то осталось; `doctor`
  сообщает о строках под неактивным ключом.
- Контракт `Store`: `rotate_key(envelope, table=, limit=, after=)` → `RotationBatch`,
  новый `inactive_key_usage(active)`.
- Мастер-ключи: `key_id` — `[A-Za-z0-9_.-]{1,64}`; ключ из одинаковых байтов в
  `IDENTITY_HUB_KEK` и файле отклоняется; BOM игнорируется; `keys()` провайдера — отображение
  только для чтения; провайдер нельзя сериализовать `pickle`.
- Публичный контракт `IdentityHub` зафиксирован ([карта API](docs/guide/api.md#контракт-хаба)):
  `store`, `envelope`, `import_runner`, `metric_rollups`, `rollups`, `state_snapshots`,
  `webhook_dispatcher`, `notification_dispatcher`, `metrics_reporter` стали приватными. Добавлены
  `hub.sessionmaker` и `hub.webhook_policy`.
- `Lease.proxy` возвращает `SecretValue`; строка URL — `lease.proxy.reveal()`. `LoginContext.proxy_url`
  и `ProbeContext.proxy_url` остаются строкой.
- `space.proxies.url(proxy_id, *, actor, reason)` и `space.secrets.reveal(..., actor, reason)`:
  автор и причина обязательны и непусты.
- `lease.check()`, `credentials()`, `session()`, `save_session()` и `lease.proxy` после освобождения
  аренды бросают `LeaseLost`; чтение секретов сверяет fencing и замечает отзыв до heartbeat.
- `repr` `ImportRecord`, `ParsedProxy` и `LeaseRow` не содержит credentials, сессий, URL и пароля
  прокси, `token` аренды.
- `WebhookEndpoint.url` — `scheme://host/***`: путь и query URL вебхука наружу не отдаются.
  Имя получателя без точки (`redis`, `metadata`) отклоняется при создании endpoint'а.
- HTTP-клиенты библиотеки (вебхуки, Telegram) не читают `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`.
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
- Ошибки драйвера БД больше не выходят из библиотеки: lock/statement timeout, deadlock и конфликт
  сериализации дают `DatabaseBusy`, остальное — `StorageError`. `token` аренды и параметры
  запросов не попадают в тексты исключений, предупреждения heartbeat/release и отчёт импорта.
- SSRF через вебхуки: URL с IPv4 в десятичной, шестнадцатеричной, восьмеричной и сокращённой
  записи (`2130706433`, `0x7f000001`, `0177.0.0.1`, `127.1`) отклоняется; имя резолвится перед
  соединением, каждый адрес проверяется, соединение идёт с проверенным IP (DNS rebinding).
- Токен бота Telegram и путь URL вебхука больше не попадают в лог `httpx`; у
  `TelegramUnavailable` и ошибки `webhooks.send_test` нет исключения `httpx` в цепочке.
- `pool.lease(wait=...)` и `space.sync.refresh()` под `FakeClock` больше не зависают; сдвиг
  `FakeClock` внутри аренды больше не даёт ложный `LeaseLost("heartbeat")`.
- Чистильщик истёкших аренд отбирает аренды с открытой историей (миграция `0011`, индекс
  `ix_ih_lease_history_open_space`): раньше более 500 давно освобождённых аренд заслоняли
  брошенную, и она оставалась открытой навсегда.
- Удаление аккаунта закрывает историю его истёкшей, но ещё не обработанной аренды.
- Один неудачный heartbeat больше не отменяет аренду: продление повторяется с коротким backoff
  в пределах дедлайна `ttl − safety_margin`.
- Повторная отмена задачи во время освобождения больше не оставляет аренду висеть до TTL:
  запись идёт под `asyncio.shield` с таймаутом, а неудавшееся освобождение можно повторить.
- `rotate-key` больше не завершается «успешно», оставив строки под старым ключом (пропуск
  заблокированных строк и выход по неполной пачке); одна нечитаемая строка не обрывает пачку.
- Фоновый импорт после удаления старого ключа: прогон завершается `failed`, а не `succeeded`,
  и нерасшифрованные записи не удаляются.
- Ошибка формата мастер-ключа больше не печатает материал ключа (перепутанный порядок
  `base64:key_id`) в исключении и stderr CLI.
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
