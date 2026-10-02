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
- `await hub.aclose()` и `async with hub:` — штатное завершение: запись накопленных метрик и
  потерь аренд, закрытие HTTP-клиента Telegram, созданного хабом. `hub.flush_metrics()` — запись
  телеметрии операций в процессе без dispatcher.
- `identity-hub rollups --rebuild --force` — перестроить диапазон старше срока хранения событий.
- `identity-hub doctor renormalize --service <имя>` — пересчёт `normalized_ref` из командной строки.
- `verifications.review(..., "reset_attempts")` — обнулить счётчик попыток фактора.
- `imports.run(..., max_identities=3)` — ёмкость прокси, создаваемых импортом.
- `space.proxies.replace_credentials()`, `move()`, `delete(unbind=)`; события `proxy.updated`
  и `proxy.deleted`.
- `space.proxies.recheck_group(group, checker, actor=...)`; `ProxyHealthPolicy.jitter`,
  `check_timeout`, `check_concurrency`; событие `proxy.checker.failed`.
- `Service(probe_timeout=...)` (по умолчанию 60 с) и `ReconcileReport.skipped_unavailable`.
- `Service(login_timeout=...)` (по умолчанию 120 с) и `space.maintenance.expire_logins()`:
  `logging_in` без живой аренды возвращается в `no_session`.
- Пауза канала уведомлений после ответа 429 (`ih_notification_channels.paused_until`,
  миграция `0012`).
- `WebhookPolicy.concurrency` (по умолчанию 8) — число endpoint'ов, обслуживаемых одновременно.
- `hub.events.subscribe(..., max_attempts=5)` и журнальное событие `dispatcher.event.skipped`.
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

- Поиск аккаунтов: строки сортируются по кодам символов (`COLLATE "C"`), а не по локали базы;
  поле профиля (`profile:<key>`) сортируется как текст на обоих хранилищах; строка запроса
  длиннее 1000 символов или с более чем 50 условиями — `ValidationFailed`.
- `hub.start()` принимает схему любой ревизии из `SCHEMA_COMPATIBLE` (сейчас `0014`–`0016`), а не
  только последней: новый код можно выкатить до `migrate`, обновление идёт без простоя.
  Сообщение `SchemaIncompatible` говорит, что делать: `identity-hub migrate` или обновить пакет.
- `migrate`: каждая ревизия применяется отдельной транзакцией (сбой не откатывает предыдущие),
  блокировка от параллельного запуска держится на сессии — запускайте миграции через прямое
  подключение, не через PgBouncer в режиме транзакций. Имя `db_schema` проверяется как
  идентификатор (`ValidationFailed`). Миграция `0016` пересчитывает `ih_proxies.bound_count`
  из привязок.
- Миграция `0015` и чистка по срокам хранения. Обслуживание теперь удаляет квитанции записи
  метрик, закрытые вопросы платформы, часовые и суточные корзины аналитики — новые поля
  `Retention.metric_batches` (7 дней), `closed_challenges` (30 дней), `rollup_hours` (90 дней),
  `rollup_days` (730 дней). После обновления первое суточное обслуживание удалит часовые корзины
  старше 90 дней и суточные старше двух лет: если они нужны дольше, задайте сроки явно.
  Удаление идёт пачками по `MaintenanceSchedule.cleanup_batch_size` строк в отдельных
  транзакциях. Вместе с событием удаляются его доставки вебхуков.
  `Store.cleanup_maintenance` получил параметр `batch_size`.
- Миграция `0014`. Доставки вебхуков отключённого endpoint'а получают статус `paused`
  (`DeliveryStatus.PAUSED`) и возвращаются в `pending` при включении endpoint'а или `retry`.
  Добавлены внешние ключи `ih_notifications.channel_id` и `ih_verifications.via_identity_id`,
  ограничения на числа прокси (`port` 1–65535, `max_identities >= 1`, `bound_count >= 0`) и индексы
  под внешние ключи. Прокси с портом вне диапазона остановит миграцию — исправьте порт заранее.
  URL прокси с портом 0 отклоняется с `ValidationFailed`.
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
- CLI: `rollups --rebuild`, `rollups --purge-minutes` и `snapshots --purge` требуют `--yes`;
  без него — код 2 без подключения к базе. Сбой фабрики и непредвиденная ошибка команды —
  код 1 и `error[factory_failed]` / `error[internal]` с именем типа исключения (раньше
  `ValueError`/`TypeError` печатались текстом как ошибка аргументов с кодом 2). `migrate`
  предупреждает о пароле в `--url`.
- Rollups: `rebuild` диапазона старше `Retention.events` — `ValidationFailed` без `force=True`
  (в CLI — `--force`); телеметрию (`telemetry.*`) перестройка больше не удаляет. Буфер метрик
  ограничен 10 000 ключей: при переполнении отбрасываются самые старые минуты. Контракт `Store`:
  новый `event_rollup_rows(space_id, bucket_size, start, end)`.
- `Override`: ключ региона проверяется — для `standing`, `availability`, `lifecycle` он пуст,
  для `auth` по умолчанию `"default"`, для `verification` — объявленный фактор; иначе
  `ValidationFailed`. `key` может быть `None`.
- Верификации: `approve` в режиме `MANUAL` — только из `under_review`; снятие `verified` по
  наблюдению требует двух наблюдений подряд; фактор с вышедшим сроком действия не даёт допуска
  до прогона обслуживания. Контракт `Store`: новый `expired_factors(identity_id)`.
- Импорт: несуществующая связь и переполненный прокси отклоняют только свою запись; фоновый
  `update_session` учитывает `verify_sessions` и для существующих аккаунтов (`auth → no_session`);
  `identity.imported` пишется и для пачки из одних обновлений; `run_files` кладёт ошибку файла
  или импортёра в `failed`; причина `ValidationFailed` схемы называет поля.
- Контракт `Store`: новые `add_proxy_error`, `proxy_by_fingerprint`, `set_proxy_secret`,
  `delete_proxy`.
- Прокси: `dead` автоматически перепроверяется раз в `max_backoff`; `recheck_due` проверяет
  параллельно; закрепление (`pin_for`) защищает от плановых перепроверок, перехода в `dead` и
  таймера региона. Контракт `Store`: `due_proxies(limit, pool=)` отдаёт и `dead`, без
  закреплённых.
- Таймеры обслуживания берут только состояния с переходом по истечении (`limited`, `paused`,
  `cooling`); `until` у `frozen`, `banned` и `quarantined` — справочный. `MemoryStore` больше не
  выдаёт в аренду `quarantined` с истёкшим `until`.
- `sync.refresh_many` сверяет только `ready`-аккаунты, очередь идёт по времени последней
  попытки (колонка `ih_identities.probe_attempted_at`, миграция `0013`); недоступные аккаунты —
  в `skipped_unavailable`, без расхода `limit`. `ProbeResult` отклоняет время без таймзоны.
- Контракт `Store`: новый `mark_probe_attempts(ids)`; `stale_identities` отдаёт только `ready`.
- Вход: `auth.login` занятого аккаунта сразу бросает `LeaseUnavailable` (раньше ждал TTL);
  `logging_in → session_invalid` — допустимый переход; ответ на challenge стирается при его
  закрытии; override или команда из `challenge`/`logging_in` закрывает открытый challenge.
- Контракт `Store`: новый `stuck_logins(limit)`; `resolve_challenge` стирает шифртекст ответа.
- Уведомления: ответ 429 не увеличивает номер попытки; дайджест отправляется частями до 4096
  символов, одиночное сообщение обрезается с «…»; шаблон с вложенным полем в формате
  отклоняется; `update_telegram_channel(thread_id=None)` сбрасывает тему; `rules()` отсортированы
  по имени.
- Контракт `Store`: `finish_notification(..., pause_channel=)`.
- `WebhookPolicy`: `timeout` обязан быть меньше `claim_for`. Pump-ы диспетчера (вебхуки,
  уведомления, импорт, обслуживание) выполняются одновременно.
- Контракт `Store`: новый `release_webhook_deliveries(claims, retry_after=)`.
- Контракт `Store`: обработчик `process_events` может вернуть число обработанных событий —
  курсор встаёт за последним из них.
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

- Рецепт подключения через PgBouncer в режиме транзакций в `docs/integrations.md` исправлен:
  прежний (`prepared_statement_cache_size: 0`) давал ошибки `prepared statement … does not
  exist` под нагрузкой. Рецепт проверяется в CI.
- `dispatcher` за PgBouncer больше не держит соединение под LISTEN, на который уведомления не
  приходят: подписка проверяется пробным уведомлением, при неудаче диспетчер освобождает
  соединение, работает опросом и пишет об этом в лог.
- `hub.background_snapshot(allowed_spaces=[...])` больше не падает на PostgreSQL с `TypeError`.
- Очередь оператора открывается при любом числе аккаунтов «на внимание»: раньше после 32 767
  запрос упирался в предел параметров asyncpg. Списки id передаются в запросы одним
  параметром-массивом.
- `doctor.check` читает аккаунты страницами по 1000 и сверяет их с регионами за линейное время;
  раньше грузил всю базу в память и работал квадратично (14 000 аккаунтов — 139 с).
- Вторая страница списка аккаунтов при сортировке по числовому или булеву полю профиля больше
  не падает; изменённый курсор даёт `ValidationFailed`, а не ошибку базы.
- `id:<hex>` длиннее 12 символов находит аккаунт и на PostgreSQL.
- `migrate` работает без права `CREATE` на базу, если схему заранее создал DBA, и с `pg_trgm`,
  установленным в схему вне `search_path` (например, `extensions`).
- Миграция `0008` заполняет `normalized_ref` одним запросом вместо построчного обновления.
- Чистка по срокам хранения больше не читает таблицы целиком и не удаляет всё одной
  транзакцией. Доставка вебхука, событие которой уже удалено, больше не захватывается
  диспетчером на каждом круге.
- Фоновые запросы на большом парке: выбор кандидатов сверки и снятие временных состояний идут
  по индексам, сводка обзора считает аренды и открытые вопросы одним проходом. На 300 000
  аккаунтов выбор кандидатов сверки — 0,01 с вместо 3,9 с, обзор — 0,4–0,9 с вместо 2,6–3,2 с.
  Очередь отключённого вебхук-endpoint'а больше не перебирается при каждом захвате доставок.
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
- Процессы `identity-hub maintenance` и `identity-hub dispatcher` больше не завершаются от
  ошибки базы на очередном тике: ошибка логируется, повтор идёт с паузой от 1 до 60 секунд.
- Диспетчер больше не перечитывает события целых суток на каждую пачку: пересчитываются
  только затронутые минутные корзины. `rollups --rebuild` не стирает телеметрию операций.
  Процесс без dispatcher не копит буфер метрик бесконечно.
- Override верификации в `verified` теперь даёт допуск: `verified_factors` согласуется.
  `Override("standing", "typo", "banned")` больше не банит аккаунт мимо строки региона, а
  override `auth` без ключа обновляет `auth_state`.
- Одна запись больше не роняет импорт: негодный URL прокси давал сырой `ValueError` с фрагментом
  пароля, обрывал `imports.run` без отчёта, а в фоновом режиме навсегда блокировал очередь
  импорта пространства и задерживал остальные пространства.
- Фоновый импорт с `update_session` больше не показывает обновлённые записи как `failed`.
- Импорт больше не привязывает к прокси аккаунты сверх `max_identities`.
- Параллельные `proxies.report_failure` больше не теряют приращения `error_score`: сложение
  выполняется в хранилище одной командой.
- Простой провайдера прокси или контрольного адреса чекера больше не переводит всю группу в
  `dead`: исключение чекера и одновременный отказ всех прокси группы отказом прокси не считаются.
- Обслуживание больше не останавливается из-за одного аккаунта: строки с `until` без перехода
  не заслоняют очередь таймеров, аккаунт незарегистрированного сервиса пропускается с метрикой,
  сбой одной группы задач не мешает остальным.
- `sync.refresh_many` больше не тратит весь лимит на `provisioning`-аккаунты и на аккаунты со
  стабильно падающей пробой; `refresh_many(["verification"])` для сервиса без факторов не падает
  с `IndexError`; `MemoryStore` больше не отдаёт `retired`-аккаунты на сверку.
- Вход больше не застревает в `logging_in`: сессия не по схеме, сбой записи и отмена задачи
  возвращают `auth` в состояние до входа. Вход дольше 40 с больше не теряет сессию — аренда
  продлевается heartbeat'ом.
- Массовое событие больше не теряет уведомления Telegram из-за лимита Bot API, большой
  дайджест не умирает целиком с HTTP 400.
- Повтор пачки событий диспетчером больше не дублирует уведомления без дайджеста: уникальность
  `(rule_id, событие)`, миграция `0012` (накопленные дубли удаляются).
- `MemoryStore`: `sent_at` уведомления, `NotFound` для правила с несуществующим каналом,
  доставки отключённого канала не заслоняют живые.
- Медленный получатель вебхука больше не блокирует диспетчер: доставка идёт параллельно, а
  пачка, не уложившаяся в срок захвата, не доставляется повторно другим экземпляром.
- Результат доставки больше не стирает ручную причину отключения endpoint'а; в `MemoryStore`
  доставки отключённого endpoint'а не заслоняют живые.
- «Ядовитое» событие больше не останавливает подписчика навсегда: курсор колбэка сдвигается
  по одному событию, обработанные события не повторяются, после лимита попыток событие
  пропускается с записью в журнал.
- Параллельные изменения одной identity (`add_tags`, слияние меток и профиля, заметка,
  требования, `verified_factors`) больше не затирают друг друга: строка блокируется до чтения.
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
