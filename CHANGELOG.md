# Changelog

Значимые пользовательские изменения Identity Hub фиксируются в этом файле.
Формат основан на Keep a Changelog; до первого стабильного релиза API может
меняться без периода совместимости.

## [Unreleased]

### Added

- `LoginFailure(code, retryable=, fault=, raw_status=)` и `LoginFailureCode`: адаптер входа
  сообщает безопасный машинный отказ, видимый в карточке, событии и durable operation.
- `Fault.until` для временных `limited` и `banned`; бан со сроком автоматически снимается
  обслуживанием, бессрочный бан остаётся sticky.
- `secrets.replace_credentials(..., reset_auth=True)` атомарно очищает старый код отказа в
  `no_session`/`session_invalid`, не отзывая сессию.

### Fixed

- `identity.login.failed` всегда содержит `code`, а пересчёт `logins.failed` использует прежнее
  поле `error` для старых событий без кода.
- Наблюдения и ошибки операций больше не выводят standing из терминального `deleted`; выйти
  можно только явным override с причиной.

### Changed

- Пробы `sync.refresh`/`refresh_many` идут через долговечные операции `probe:<region>`
  (`IMP-11`, `D-223`, миграция `0032`): claim фиксируется до вызова адаптера, итог и наблюдение
  применяются атомарно, поздний ответ отклоняется, а параллельная живая проба возвращает `busy`.
  Maintenance завершает истёкшие claim без повторного сетевого вызова.

## [1.0.0a2] - 2026-10-12

Второй альфа-релиз: доработки по итогам встраивания в приложение (очереди `N` и `C`). Ревизия
схемы — `0031`: перед обновлением примените `identity-hub migrate`.

### Added

- Админка: массовые операции (`C2.2b`). Ревизии пачки сверяются одним запросом
  (`identities.get_many`) вместо карточки на каждый аккаунт; массовая смена жизненного цикла в
  `ready` и `retired` идёт пакетной командой ядра одной транзакцией. Формат отчёта прежний.
- Админка: выключение прокси (`C1.4b`). В карточке и списке прокси — `enabled`, причина, автор и
  время выключения; фильтр `enabled` в `GET /proxies`; число выключенных в ёмкости групп;
  команды `POST /proxies/{id}/commands/disable` (с подтверждением) и `/commands/enable` для
  ролей operator и admin. `space.admin.proxy_overview(filters=ProxyFilters(enabled=...))`.
- CLI (`C6.2`): `identity-hub keygen [--key-id k1]` печатает мастер-ключ для
  `IDENTITY_HUB_KEK`; `identity-hub migrate` берёт адрес из
  `IDENTITY_HUB_MIGRATE_DATABASE_URL` раньше общего — для миграций в обход PgBouncer. Ошибка
  создания хаба без мастер-ключа называет команду и переменную окружения.
- Режим миграций при старте (`C6.1`): `IdentityHub(..., migrate="check" | "auto" | "off")`.
  `"check"` (по умолчанию) — прежняя сверка ревизии; `"auto"` — `hub.start()` сам применяет
  миграции под блокировкой миграции; `"off"` — без сверки.
- Хук импорта внутри транзакции пачки (`C5.1`): `imports.run(..., on_batch=hook)` и
  `run_files`. `hook(session, created)` вызывается перед фиксацией пачки; строки приложения,
  записанные через `session`, фиксируются вместе с аккаунтами, исключение хука откатывает
  пачку и кладёт её записи в `report.failed`. `on_batch_committed` работает как раньше.
- Руководство: состав `data` каждого типа события — таблица в `docs/guide/10-events.md`
  (`C4.3`). Тесты сверяют её с событиями, которые пишет библиотека: новый ключ без правки
  документации роняет прогон.
- Параметр `start="now"` у `hub.events.subscribe` и `subscribe_batch` (`C4.2`): новый
  подписчик начинает с текущего момента, а не с начала хранимого журнала. У подписчика с
  существующим курсором параметр ничего не меняет.
- `hub.events.subscribe_batch(name, handler, types=, space=, service=, max_attempts=)` —
  подписка, которая отдаёт события пачкой (`C4.1`). При сбое обработчика пачка делится
  пополам до одного события; сбойное событие после `max_attempts` неудач пропускается с
  записью `dispatcher.event.skipped`, как у `subscribe`. Фильтры `space` и `service` получила
  и `subscribe`.
- `identities.get_many(ids)` → `{id: Identity}` одним запросом, до 1000 аккаунтов (`C3.5`):
  состояния пачки без read-схемы, одинаково на PostgreSQL и `MemoryStore`. В руководстве по
  тестированию описано, что `hub.read.*` работает только с PostgreSQL.
- Карточка аккаунта: у окна квоты появился `limit` — лимит из правила операции сервиса рядом
  с расходом `used` (`C3.4`). `None` — правила с таким окном в описании сервиса нет.
- `identities.tags(service=None, limit=1000)` → список `TagCount` (`tag`, `count`): теги
  пространства с числом аккаунтов, по убыванию числа (`C3.3`).
- Пакетные команды аккаунтов (`C2.1`, `C2.2a`): `identities.activate_many`, `retire_many`,
  `restore_standing_many`, `override_many`, `delete_many` → `BatchResult`. Пачка до 1000 аккаунтов идёт одной
  транзакцией (под `space.using(session)` — в транзакции приложения); событие и ревизия — на
  каждый аккаунт; отказ одного не отменяет остальные, `atomic=True` отменяет всю пачку.
- `proxies.unbind(identity_id, reason=, actor=)` — снять привязку аккаунта к прокси, и
  `proxies.rebind_many({identity_id: proxy_id}, reason=, actor=, atomic=)` → `BatchResult` с
  отказами по аккаунтам: нет места, прокси не `alive` или выключен (`C1.5`). Событие
  `proxy.binding.changed` — на каждый аккаунт.
- Выключение прокси оператором (`C1.4a`, `D-224`, миграция `0025`): `proxies.disable(proxy_id,
  reason=, actor=, unbind=)`, `proxies.enable(proxy_id, actor=)`, `proxies.set_enabled_many(ids,
  enabled, actor=, reason=)`, фильтр `proxies.list(enabled=)`. Флаг `enabled` независим от
  здоровья: выдаётся только включённый и живой прокси, автоматика выключенный не трогает,
  статус при включении сохраняется. Привязанные аккаунты остаются привязанными и не выдаются
  при любой `on_proxy_down`; `unbind=True` отвязывает их. Поля `enabled`, `disabled_reason`,
  `disabled_by`, `disabled_at` — в `Proxy` и `hub.read.proxies`; события `proxy.disabled` и
  `proxy.enabled`. `hub.start()` требует ревизию схемы `0025`.
- `proxies.update(proxy_id, scheme=, host=, port=, username=, password=, actor=)` — смена адреса
  прокси без удаления (`C1.3`): привязки, статус, счётчики и группа сохраняются, отпечаток
  пересчитывается, занятый адрес — `AlreadyExists`. Событие `proxy.updated` несёт прежний адрес.
- Пакетные команды прокси (`C1.2`): `proxies.delete_many(ids, actor=, unbind=, atomic=)` →
  `ProxyDeleteManyResult` (`applied`, `rejected`, `unbound`), `proxies.override_many({id: статус},
  reason=, actor=, pin_for=, atomic=)` → `BatchResult`, `proxies.report_many({id: True | False |
  вес}, actor=)` → статусы после отчёта. Удаление и смена статуса идут одной транзакцией (под
  `space.using(session)` — в транзакции приложения); события те же, что у одиночных команд.
- `proxies.create_many` принимает структурные записи `ProxyCreate` (адрес по частям, свой `id`,
  своя ёмкость) вперемешку с URL и возвращает `result.items` — исход каждой записи в порядке
  входа (`ProxyCreated`: `index`, `id`, `created`) (`C1.1`). Вставка и чтение идут пачкой: число
  запросов не зависит от размера, потолок — 1000 записей. Первый параметр переименован из
  `urls` в `items`.
- Пакетный контракт (`C0.2`, `D-224`): `BatchResult` (`applied`, `rejected`) и `BatchRejection`
  (`key`, `code`, `message`) — общий результат команд над множеством сущностей. Пачка идёт одной
  транзакцией (под `space.using(session)` — в транзакции приложения), ключи блокируются по
  возрастанию, потолок — 1000. По умолчанию отказ одной сущности откатывает только её точку
  сохранения; `atomic=True` отменяет всю пачку. Команды на этом контракте появляются в
  следующих изменениях.
- Read-схема: `hub.read.proxy_bindings` (`identity_id`, `proxy_id`, `space_id`, `bound_at`) —
  sticky-привязки аккаунт ↔ прокси для join со списком аккаунтов (`C3.1`). Версия read-схемы
  не меняется.
- GCRA-троттлинг (`N4.1`): `Service(throttles={"api": Throttle(...)})` и
  `await lease.throttle(name, wait=...)`; эксклюзивный держатель считает без запроса на вызов,
  а миграция `0031` сохраняет TAT между арендами.
- Надёжность троттлинга (`N4.2`): после падения держателя TAT восстанавливается консервативной
  верхней границей; `SHARED`-аренды используют один атомарный GCRA-счётчик в БД на вызов.
- Приоритет аренды (`N3.2a`): `pools.define(priority=)` и разовый `pool.lease(priority=)`;
  более приоритетный запрос атомарно помечает одну низкоприоритетную аренду, а держатель получает
  `lease.yield_requested` через `renew()`/heartbeat. Миграция `0029` хранит приоритет и запрос.
- Кооперативная уступка (`N3.2b`): `on_yield_requested`, причина истории `yielded` и опциональный
  `pools.define(yield_grace=)` с принудительной уступкой и короткой приоритетной бронью. Миграция
  `0030` хранит бронь на identity.
- Резерв ёмкости пула (`N3.1`, задача 2 из `ISSUES-INTEGRATION.md`):
  `pools.define(reserve=, reserve_strict=)`, разовый `pool.lease(keep_free=)`, атомарная выборка
  `N + 1` кандидатов и `LeaseUnavailable(reason="reserve")`. `pool.capacity_detail()` возвращает
  точные `free`, `reserved` и `available`; `SHARED` резерв не поддерживает.
- Атомарные массовые изменения (`N2.3`, задача 7 из `ISSUES-INTEGRATION.md`):
  `identities.update_many()` меняет теги, метки и lifecycle по `IdentityFilter` или списку UUID,
  ограничивает охват через `limit`, блокирует строки в порядке id и откатывает всю пачку при
  любой ошибке. Админские bulk-команды lifecycle/тегов используют этот метод.
- Команды состояния и публичные наблюдения (`N2.2`, задача 7 из `ISSUES-INTEGRATION.md`):
  `identities.ban()`, `freeze()` и `cool_down()` дают именованные операции поверх существующей
  машины регионов; `identities.observe()` принимает экспортируемый из корня `Observation` и
  проводит внешние факты через те же правила stale/pin/sticky/confirmation, что операции и пробы.
- Ручное продление аренды (`N2.1`, задача 7 из `ISSUES-INTEGRATION.md`): `await lease.renew()`
  продлевает аренду из `pool.acquire()`, возвращает новый `expires_at`, соблюдает `max_hold` и
  `stats_flush`, а при потере бросает `LeaseLost` с причиной. В управляемой аренде работает как
  внеочередной heartbeat.
- Стабильная стоимость выборки с квотами (`N1.3b`): отдельный `pick_order_at` сохраняет смысл
  публичного `last_leased_at`, а ограниченный перенос исчерпанной головы LRU не даёт десяткам
  тысяч исчерпанных аккаунтов замедлять `require_quota`. Миграция `0028` переводит индекс
  `ix_ih_identities_pick` на новый порядок.
- Учёт квот при выборе аккаунта (`N1.3`, задача 5 из `ISSUES-INTEGRATION.md`):
  `pool.lease(for_operation=..., require_quota=True)` и то же у `pool.acquire()` не выдают аккаунты
  с исчерпанным окном квоты операции. Если мешает только квота — `LeaseUnavailable` с
  `reason="quota"` и `retry_after` до ближайшего открытия окна. По умолчанию поведение прежнее.
- Потолок удержания аренды (`N1.2`, задача 4 из `ISSUES-INTEGRATION.md`): `pools.define(max_hold=)`,
  `pool.lease(max_hold=)`, `pool.acquire(max_hold=)` и `lease.hold_deadline`. Продление не ставит
  срок позже дедлайна; держатель получает `LeaseLost` с причиной `max_hold`, в истории —
  `LeaseEndReason.MAX_HOLD`. Дедлайн есть в списке живых аренд и read-схеме (версия 4). Миграция
  `0027` добавляет `hold_deadline` в `ih_leases` и `ih_lease_history`.
- Назначение аренды (`N1.1`, задача 6 из `ISSUES-INTEGRATION.md`): `pool.lease(purpose=, ref=)`,
  `pool.acquire(purpose=, ref=)` и значение по умолчанию `pools.define(purpose=)`. Поля видны как
  `lease.purpose` и `lease.ref`, в истории, событиях `lease.*`, read-схеме (версия 3) и админке;
  `lease_dashboard(group_by="purpose")`. Миграция `0026` добавляет колонки в `ih_leases` и
  `ih_lease_history`.
- Причина отказа и причина потери аренды (`N0.1`, задачи 1, 4, 5 из `ISSUES-INTEGRATION.md`):
  `LeaseUnavailable.reason` — `busy`, `no_candidates` или `no_proxy` (`LeaseUnavailableReason`);
  держатель, чья аренда истекла до heartbeat, получает `LeaseExpired` с причиной `expired`, а не
  `LeaseRevoked`. Контракт хранилища: `StoreTx.extend_lease` и `Store.renew_leases` возвращают
  `LeaseRenewal(expires_at, end_reason)` вместо `datetime | None`.

### Changed

- Теги принимают буквы и цифры любого письма, в том числе кириллицу, и знаки `_ . : -`
  (`C3.2`); раньше — только латиница. Длина — до 64 символов, регистр значим. Теги приводятся к
  форме Unicode NFC при записи, в фильтрах поиска и в отборе пула. Прежние теги остаются
  допустимыми.

### Fixed

- Команды прокси под `space.using(session)` (`C0.1`): `proxies.create`, `ensure`, `create_many`,
  `replace_credentials` и `move` возвращают строку, прочитанную в транзакции записи. Раньше
  результат читался отдельной транзакцией: `create` до фиксации падал с `NotFound`, `move`
  возвращал прежнюю группу.

## [1.0.0a1] - 2026-10-12

Первый публичный альфа-релиз. API может меняться между альфа-версиями. Изменения ниже
описаны относительно внутреннего среза от 2026-09-30.

Исправлены 118 из 125 находок предрелизного ревью; что чем закреплено — раздел «Статус
исправлений» в `docs/review/PRE-RELEASE-REVIEW.md`, оставшиеся 7 описаны в
[ограничениях](docs/guide/limitations.md#известные-дефекты).

### Added

- Очередь операций провайдера (`IMP-11`, `D-220`, миграция `0024`): машина состояний и
  `RetryPolicy` в `identity_hub.domain.provider_operations`; Store `enqueue_provider_operation`,
  `claim_provider_operations`, `apply_provider_operation`, `recover_provider_operations`,
  `cancel_provider_operation`. Гарантия — at-least-once попытка. `hub.start()` требует ревизию
  схемы `0024`.
- Режимы записи сессии (`IMP-10`, `D-219`): `lease.save_session(..., mode=)` и
  `secrets.replace_session(..., mode=)` — `in_place` (по умолчанию) или `replace` (новая запись,
  прежняя в истории); `secrets.revoke_session` — отзыв без замены. Вход сохраняет сессию в режиме
  `replace`. События `session.saved` и `identity.session.updated` несут `mode`, `session_id`,
  `replaced_id`, `session_revision` — без данных сессии.
- `CredentialInput` и `credential_records` у `IdentityCreate`, `ImportRecord` (в том числе
  отложенного импорта) и `make_identity`; `AccessCard.credential_records` — все записи без
  секретов (`IMP-09`, `D-217`).
- `lease.credential(kind, name)`, `lease.active_credentials()`, `ctx.credential(kind, name)` в
  контекстах входа и проб, `SecretRef.credential(kind, name)` для раскрытия с аудитом
  (`IMP-09`, `D-216`). `lease.credentials()` остаётся доступом к записи `default` и, как и
  `credential()`, не отдаёт выключенную, отозванную или истёкшую запись.
- Команды нескольких credentials (`IMP-09`, `D-215`): `space.secrets.put_credential`,
  `credentials` (метаданные без секрета, `Credential` с `expired` по времени),
  `disable_credential`, `enable_credential`, `revoke_credential`; `revoked` — конечное состояние.
- `idempotency_key` у `secrets.replace_credentials` и `secrets.replace_session`: секрет входит в
  отпечаток команды только HMAC-тегом ключом отпечатков, после ротации повтор сверяется прежним
  ключом (`IMP-04`, `D-208`, миграция `0019`). `hub.equivalence_tag(value)` — HMAC-тег значения
  для сравнения чувствительных данных без их хранения.
- Кольцо ключей отпечатков (`IMP-04`, `D-207`): HMAC-SHA-256 отдельными ключами, не KEK.
  `StaticSecretsProvider(fingerprint_keys=, fingerprint_active=)`, переменная
  `IDENTITY_HUB_FINGERPRINT_KEYS`, `FileSecretsProvider(fingerprint_path=)`; без них ключи
  выводятся из KEK через HKDF. `hub.doctor.rekey_fingerprints()` и
  `identity-hub doctor rekey-fingerprints` пересчитывают отпечатки прокси.
- `expected_revision` у команд изменения аккаунта (`space.identities`, `space.secrets`,
  `space.links`, `space.verifications`): устаревшая ревизия — `RevisionConflict` без изменений,
  в том числе для параллельной команды в другой сессии (`IMP-07`, `D-206`). Руководство —
  [«Аккаунты»](docs/guide/03-accounts.md#параллельные-правки-expected_revision).
- Раздел «Что входит в публичный контракт» и таблица значений по умолчанию в
  [«Карте API»](docs/guide/api.md) (тест сверяет таблицу с кодом); краткий раздел README на
  английском: назначение, установка, статус альфа, язык документации.
- `SecretValue`: секрет со скрытым `repr`/`str`, без JSON и pickle; scrubber событий его отвергает.
- Ресурсные неймспейсы пространства (`space.identities`, `space.imports`, `space.secrets`,
  `space.proxies`, `space.pools`, `space.auth`, `space.sync`, `space.links`, `space.data`,
  `space.verifications`, `space.admin`, `space.analytics`, `space.maintenance`) с единой
  конвенцией глаголов; карта — [«Карта API»](docs/guide/api.md).
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

- Вход идёт через очередь операций провайдера (`IMP-11`, `D-222`): claim коммитится до вызова
  `Authenticator`, итог — одной транзакцией с проверкой claim и fence; поздний ответ зависшего
  воркера ничего не пишет. `ctx.attempt_id` — ключ идемпотентности попытки,
  `LoginReport.operations` — операции попыток. Store: `claim_provider_operations(operation_ids=)`.
- Сессии хранят историю (`IMP-10`, `D-218`, миграция `0023`): у записи есть `id` и ревизия, в
  слоте одна не отозванная запись. `SessionStatus`: `created`, `active`, `invalidated`,
  `revoked` (`VALID`/`INVALID` — псевдонимы `ACTIVE`/`INVALIDATED`, `EXPIRED` удалён; прежние
  строки переведены миграцией). Store: `put_session(..., mode="in_place"|"replace")` возвращает
  `SessionWrite` или `None`, `revoke_session`, `session_history`. `hub.start()` требует ревизию
  схемы `0023`.
- `ih_credentials` хранит несколько записей на аккаунт (`IMP-09`, `D-214`, миграция `0022`): вид
  (`kind`), имя (`name`), открытая часть, жизненный цикл, срок и ревизия; прежняя запись стала
  `default`/`default` без перешифрования. `Service.credential_kinds` объявляет виды кроме `default`
  (`credential_schema`, `credential_key`); Store: `put_credential`, `get_credential`,
  `list_credentials`; `credential_associated_data` в `identity_hub.crypto`. `hub.start()` требует
  ревизию схемы `0022`.
- Семантика renew/release/revoke (`IMP-06`, `D-213`): heartbeat не укорачивает срок
  (`GREATEST`); `release()` отозванной аренды бросает `LeaseRevoked`, освобождение после
  естественного истечения закрывает историю как `expired`; `pools.revoke()` сразу будит аренду
  этого же процесса. Store: `release_lease` возвращает исход (`released`/`expired`/`closed`/
  `revoked`) вместо `bool` — собственные реализации Store нужно обновить.
- `LeaseLost.body_completed`: потеря аренды после успешного тела `async with pool.lease()`
  (`R1-16`).
- Heartbeat аренд процесса продлевается пачкой: одновременные продления уходят одним
  `UPDATE ... WHERE token = ANY(...)` (`IMP-02`, `D-211`, Store `renew_leases`). Отзыв или потеря
  касается только своей аренды. Замер: 272 продления/с поштучно, 21 тыс./с пачками по 100,
  58 тыс./с по 500.
- Heartbeat эксклюзивной аренды — HOT-обновление (`IMP-02`, `D-210`): миграция `0021` удаляет
  индексы `ix_ih_leases_expires_at` и `ix_ih_lease_history_open`, у `ih_leases` — `fillfactor = 70`.
  История аренды получает время heartbeat и счётчики раз в `LeaseTiming.stats_flush` (по
  умолчанию 5 мин, `pools.define(stats_flush=)`) и при освобождении; список живых аренд берёт
  heartbeat из строки аренды. Store: `extend_lease(..., flush=)`.
- Выборка аренды — по времени последнего захвата в строке аккаунта (`last_leased_at`, миграция
  `0020`) и индексу в том же порядке (`IMP-01`, `D-209`): занятые аккаунты не перебираются, ротация
  равномерная и для разделяемых аренд. Раньше порядок шёл по строке живой аренды и после
  освобождения терялся. `hub.start()` требует ревизию схемы `0020`.
- `Identity.revision`, `IdentityCard.revision` и `hub.read.identities.c.revision` — ревизия
  агрегата: растёт на 1 за команду, изменившую аккаунт, а не на каждое событие (`IMP-07`,
  `D-205`). Аренда, операции, прокси и раскрытие секрета её больше не меняют. Миграция `0017`
  переносит прежние значения (`GREATEST(event_seq, 1)`), `READ_SCHEMA_VERSION` — 2 (`R2-D23`).
  `hub.start()` требует ревизию схемы `0017`.
- В событии — поле `revision` (ревизия аккаунта после команды): `Event.revision`, тело вебхука,
  журнал админки, колонка `hub.read.events.c.revision`. `StoreTx.lock_identity_revision` при
  расхождении бросает `RevisionConflict` (наследник `ConcurrentModification`); `doctor`
  проверяет ревизию по последнему событию.

- `ProbeContext.external_ref`; `LoginContext.session(slot)` — прежняя сессия при повторном входе,
  в том числе признанная недействительной. `FileImporter.read` может быть async-генератором,
  обычной функцией или `async def`, возвращающими список. `imports.run_files(base_dir=,
  max_bytes=)`: путь вне каталога и файл больше лимита попадают в `failed`. Примеры и руководство
  типизируют `ctx: LoginContext` / `ctx: ProbeContext` и обходятся без заглушек.
- Адаптеры сервиса проверяются при объявлении `Service`: метод на месте, `async def`, у
  `FileImporter.read` — async-генератор; сообщение называет адаптер и нужную сигнатуру.
  `Authenticator` требует только `login` (`refresh` — необязательный `SessionRefresher`);
  `probes` — любое подмножество `auth`/`standing`/`verification` (`AuthProbe`, `StandingProbe`,
  `VerificationProbe`). `Service(authenticator=login_fn)` и
  `Service(error_classifier={TimeoutError: Fault(...)})`; классификатор получает вид операции,
  если `classify` принимает `kind`. Протокол `EventConsumer` удалён: `subscribe` принимает
  `Callable[[Event], Awaitable[None]]`.
- **Несовместимо:** `actor` — обязательный именованный параметр во всех методах неймспейсов,
  которые записывают, кто выполнил действие: `identities.create/ensure/create_many/update/delete`,
  работа с тегами, профилем, атрибутами, метками и заметкой, `links.add/remove`,
  `imports.run/run_files/stage`, `proxies.report_failure`, `verifications.requirements.add/resolve`,
  `auth.answer_challenge`. Молчаливых `"system"`, `"platform"`, `"worker"` больше нет.
- **Несовместимо:** `sync.refresh` возвращает `RefreshResult` (`outcomes` — исход по региону),
  `proxies.recheck_due` и `proxies.recheck_group` — `RecheckReport` (`statuses` — новый статус
  каждого прокси).
- **Несовместимо:** `IdentityCreate.auth` вместо `auth_state` — как в `Identity` и `IdentityFilter`.
- `LeaseUnavailable` отображается в HTTP 503 (временная недоступность) вместо 404; необязательный
  `retry_after` попадает в `context`.
- `proxies.create_many(on_conflict=...)` принимает `OnConflict`; `imports.run_files` — явные
  параметры, как у `imports.run`, вместо `**options`; `among` в пулах типизирован:
  `Select | Iterable[UUID] | Callable[[Identity], bool]`.
- `space.webhooks` и `space.notifications` — неймспейсы, как остальные: один объект на пространство.
- **Несовместимо:** `hub.space(slug)` больше не создаёт пространство. Если его нет, первая
  операция даёт `NotFound` — опечатка в slug не превращается в пустое пространство. Создание —
  `hub.spaces.create(slug, title)` (уже есть — `AlreadyExists`), `hub.spaces.ensure(slug, title)`
  или `hub.space(slug, title, create=True)`; поиск — `hub.spaces.get(slug)`.
- CI проверяет, что тесты вызывают каждый метод контракта `Store` на `MemoryStore` и PostgreSQL
  (`IH_STORE_COVERAGE=1` для полного прогона).
- `StoreConformanceSuite` проверяет изоляцию пространств, учёт живой аренды при выборке, отзыв
  аренды и fencing, атомарность квот, полный откат транзакции, курсор поиска и доставку только
  внешних событий с соблюдением `limit`. Контракт `Store` объявлен внутренним и нестабильным до
  1.0 (руководство, раздел «Свой адаптер хранилища»).
- Тестовый инструментарий: `TestingModel` (модели сервиса из `make_service` по умолчанию)
  отвергает неизвестные поля; `make_identity(authenticated=False, session=...)` —
  `ValidationFailed`. `pool.lease(among=...)` принимает предикат от `Identity`.
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
- Associated data шифрования в формате `identity-hub:v1:…`; записи среза от 2026-09-30 читаются,
  `rotate-key` переводит их в новый формат. Лимиты: JSON до 64 KiB и 16 уровней, секрет до 512 KiB.
- Единый порядок блокировок ([`docs/lock-order.md`](docs/lock-order.md)) и
  `StoreTx.lock_identities()`; проверка границ импортов `lint-imports` в `scripts/check.sh`.
- `Pool.lease()` и `Pool.acquire()` принимают явные параметры: опечатка в имени — `TypeError`.
- `DecryptionFailed` наследует `SecretConfigurationError` (код `decryption_failed`).

### Fixed

- `MemoryStore` ведёт `version` и `updated_at` credentials и сессий так же, как PostgreSQL
  (`R2-P08`).
- Полный fence аренды (`IMP-05`, `D-212`, `R1-15`): запись и чтение секретов от имени аренды
  сверяют `lease_id`, `token`, точное равенство fencing и живую аренду под блокировкой строки
  аккаунта. Истёкшая аренда, которую ещё никто не перехватил, больше не пишет; отказ —
  `LeaseOwnershipLost` (наследует `LeaseLost` и `StaleFencing`). `lease.fence` и `LeaseFence` в
  корне пакета, Store `check_fence`.
- Админка: отпечаток идемпотентности чувствительных команд (токен канала уведомлений, ответ на
  challenge) считался по значениям, уже замаскированным `SecretStr`, поэтому
  повтор с тем же `Idempotency-Key` и другим секретом отдавал прежний результат вместо 409.
  Теперь секрет входит в отпечаток HMAC-тегом `hub.equivalence_tag` (`D-208`).
- Отпечаток прокси — HMAC ключом отпечатков вместо SHA-256 без ключа: логин прокси больше не
  подбирается перебором по известному хосту (`R2-S16`). Миграция `0018` добавляет
  `ih_proxies.fingerprint_key_id`; прежние отпечатки находятся до пересчёта
  `identity-hub doctor rekey-fingerprints`. `hub.start()` требует ревизию схемы `0018`.
  `identity_hub.proxies.fingerprint` заменён на `fingerprint_value` и `legacy_fingerprint`.
- `Page.total` у `identities.list`/`search` описан верно: точное число до 1000, больше — 1001;
  точное число до 1 000 000 — `identities.count()` (`R1-28`).
- Транзакция `MemoryStore` больше не копирует всё состояние: цикл аренды с операцией при 1000
  аккаунтах — около 5 мс вместо 600.
- `MemoryStore` ведёт себя как PostgreSQL: удаление аккаунта освобождает место на прокси;
  доказательства верификации читаются после `submit`; события и аренды одного момента
  упорядочены (UUIDv7); фильтр профиля `{"key": None}` не совпадает с отсутствующим полем;
  связи идут в том же порядке и с временем связи, путь `links.impact` — от исходного аккаунта;
  открытая аренда учитывается в `lease_stats` как `active`.
- Фикстура `identity_hub` pytest-плагина работает в strict-режиме pytest-asyncio (режим по
  умолчанию), а без pytest-asyncio тест останавливается с понятной ошибкой.
- `among=select(...)` на `MemoryStore` — `ValidationFailed` с подсказкой вместо `TypeError`.
- Один `MemoryStore` работает в нескольких `asyncio.run()` подряд и при конкурентных транзакциях.
- `AlreadyExists` при совпадении ref с аккаунтом другого пространства больше не содержит его id
  (`existing_id` пуст): раньше это было утечкой между пространствами.
- Границы очистки минутных корзин и снимков, час снимка и минуты метрик считаются по времени
  базы, как и корзины событий; расхождение часов процесса и базы больше не сдвигает их.
- `new_id()` строго растёт в пределах процесса при любой реализации UUIDv7; минимальная версия
  `uuid-utils` (Python до 3.14) — 1.0.
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

### Removed

- **Несовместимо:** 84 плоских метода `SpaceHandle` (`register`, `search`, `identity_card`,
  `import_many`, `reveal_secret`, `add_proxy`, `pool`, `proxy_health` и другие) удалены без
  периода совместимости: API ещё не публиковался. Вместо них — неймспейсы (`space.identities.ensure`,
  `space.identities.search`, `space.secrets.reveal`, `space.proxies.ensure`, `space.pools.define`,
  `space.proxies.health`…), см. [«Карта API»](docs/guide/api.md). У `SpaceHandle` публичны только
  неймспейсы, `using` и `ref`.
- `ProxyPolicy(pool=...)` — используйте `ProxyPolicy(group=...)`; `Pool.check_capacity()` —
  `Pool.capacity()`. Таблица миграции `docs/api-migration.md` и `identity_hub.api.warn_deprecated`
  удалены.

## Внутренний срез 1.0.0a1 - 2026-09-30

Не публиковался: под этим номером версии на PyPI вышел раздел выше.

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
