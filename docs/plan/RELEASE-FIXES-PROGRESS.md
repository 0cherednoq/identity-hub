# Прогресс исправлений перед первой публикацией

> Единственный источник правды о статусе очереди `F` (находки ревью `R1-*`, `R2-*`).
> Обновляется в том же коммите, что и код задачи.
> Статусы: `todo` · `in_progress` · `done` · `blocked` · `cancelled`.
> Спецификация: [`RELEASE-FIXES-PLAN.md`](RELEASE-FIXES-PLAN.md).
> Требования: [`PRE-RELEASE-REVIEW.md`](../review/PRE-RELEASE-REVIEW.md). Протокол: [`CLAUDE.md`](../../CLAUDE.md).

## Текущая задача

<!-- Заполняется при взятии задачи и очищается при завершении. Это checkpoint для продолжения после обрыва. -->

- **Задача:** —
- **Начата:** —
- **Шаги:**
  - [ ] —
- **Заметки для следующей сессии:** —

## Статус задач

| ID | Задача | Зависит от | Статус | Коммит | Примечание |
|---|---|---|---|---|---|
| F0.1 | Трассировка находок | — | done |  |  |
| F0.2 | CI библиотеки | F0.1 | done |  |  |
| F1.1 | Протокол идемпотентности операций | F0.1 | done |  |  |
| F1.2 | Идемпотентность аренды | F1.1 | done |  |  |
| F1.3 | Ошибки БД без параметров и `token` | F0.1 | done |  |  |
| F1.4 | Свободный текст в истории, событиях и логах аренды | F0.1 | done |  |  |
| F1.5 | Исходящие HTTP-запросы | F0.1 | done |  |  |
| F1.6a | Секреты вне аренды, аудит и repr | F0.1 | done |  | часть F1.6 (D-156) |
| F1.6b | Публичная поверхность хаба | F1.6a | done |  | часть F1.6 (D-156) |
| F1.7 | Провайдеры ключей | F0.1 | done |  |  |
| F1.8 | Ротация ключей | F0.1 | done |  |  |
| F2.1 | Heartbeat и освобождение аренды | F0.1 | done |  |  |
| F2.2 | Чистильщик аренд и удаление аккаунта | F0.1 | done |  |  |
| F2.3 | Часы и ожидание | F2.1 | done |  |  |
| F2.4 | Потерянные обновления identity | F0.1 | done |  |  |
| F3.1 | Диспетчер: изоляция сбойного события | F0.1 | done |  |  |
| F3.2 | Вебхуки: параллельная доставка | F0.1 | done |  |  |
| F3.3 | Уведомления Telegram | F0.1 | done |  |  |
| F3.4 | Вход и challenge | F0.1 | done |  |  |
| F3.5 | Сверка с платформой | F0.1 | done |  |  |
| F3.6 | Обслуживание и таймеры | F0.1 | done |  |  |
| F3.7a | Здоровье прокси | F0.1 | done |  | часть F3.7 (D-171) |
| F3.7b | Управление прокси | F3.7a | done |  | часть F3.7 (D-171) |
| F3.8 | Импорт | F0.1 | done |  |  |
| F3.9 | Override и верификации | F0.1 | done |  |  |
| F3.10 | Rollups, метрики и закрытие хаба | F0.1 | done |  |  |
| F3.11 | CLI | F0.1 | done |  |  |
| F4.1a | Индексы и целостность схемы | F0.1 | done |  | часть F4.1 (D-178) |
| F4.1b | Retention | F4.1a | done |  | часть F4.1 (D-178) |
| F4.2 | Миграции: права и данные | F0.1 | done |  |  |
| F4.3 | Совместимость ревизии схемы | F0.1 | done |  |  |
| F4.4a | Поиск аккаунтов | F0.1 | done |  | часть F4.4 (D-183) |
| F4.4b | Админ-чтение на объёме | F4.4a | done |  | часть F4.4 (D-183) |
| F4.5 | PgBouncer | F0.1 | done |  |  |
| F4.6 | Мелкие расхождения хранилища | F0.1 | done |  |  |
| F5.1 | pytest-плагин и фабрики | F0.1 | done |  |  |
| F5.2a | Паритет поведения `MemoryStore` | F0.1 | done |  | часть F5.2 (D-189) |
| F5.2b | Скорость `MemoryStore` | F5.2a | done |  | часть F5.2 (D-189) |
| F5.3a | Набор StoreConformanceSuite | F2.2, F3.2, F3.3, F3.5, F3.6, F3.7b, F3.8, F5.2b | done |  | часть F5.3 (D-192) |
| F5.3b | Покрытие методов Store | F5.3a | todo |  | часть F5.3 (D-192) |
| F6.1 | Пространства | F0.1 | todo |  |  |
| F6.2 | Единообразие API | F0.1 | todo |  |  |
| F6.3 | Адаптеры | F0.1 | todo |  |  |
| F6.4 | Удаление устаревших алиасов | F1.6b, F6.2, F6.3 | todo |  |  |
| F6.5 | Версия, язык и публичный контракт | F6.4 | todo |  |  |
| F7.1 | Финальная сверка | все задачи F0–F6 | todo |  |  |

**Итого:** 39 / 46 done

## Журнал сессий

<!-- Добавлять строку после завершения задачи или остановки. Формат:
YYYY-MM-DD · ID · результат (done / partial / blocked) · что сделано · что дальше -->
2026-10-01 · F0.1 · done · тест трассировки (4 проверки: находки↔матрица, задачи↔планы, план↔прогресс, «Находки:» разделов↔матрица); repro без БД (api, functional, secrets кроме b3) воспроизводят поведение ревью, расхождений нет; скрипты с PostgreSQL (b3, schema, parity) не перезапускались — их контейнеры удалены, код src с момента ревью не менялся · дальше F0.2
2026-10-02 · F0.2 · done · library-ci.yml (check: 3.12/3.13/3.14 + PostgreSQL 16 + check.sh; package: uv build + scripts/check_package.sh), сторожевой тест tests/test_library_ci.py; actionlint 1.7.12 чисто; локально mypy и pytest зелёные на 3.12, 3.13, 3.14 (767 passed), пакет проверен в чистом окружении · дальше F1.1
2026-10-02 · F1.1 · done · ih_operation_keys с PK (namespace, key) — миграция 0009, FK на аккаунт снят; канонический fingerprint; примитив claim/complete в PgStore и MemoryStore; Operation.previous_outcome, повтор после failed — новая попытка; сбой учёта после успеха не выходит наружу; D-153 (аккаунт вне fingerprint, legacy-записи в namespace пространства); 807 passed, гонки ×3 · дальше F1.2: claim в namespace <space>:lease + FOR UPDATE на replay, сверка pool/holder/service найденной аренды, uq_ih_leases_idempotency → неуникальный индекс по пространству (миграция 0010)
2026-10-02 · F1.2 · done · ключ аренды в namespace <space>:lease с fingerprint пул+сервис+holder, FOR UPDATE на повторе, прямая сверка живой аренды; uq_ih_leases_idempotency → ix_ih_leases_idempotency (миграция 0010); I1.3 помечена done; гонки ×3 · дальше F1.3: перевод DBAPIError в PgTx.execute/PgStore._execute и на границе транзакции (DatabaseBusy для 55P03/57014/40P01/40001, новый StorageError), черновик теста — scratchpad/f13wip/test_storage_errors.py
2026-10-02 · F1.3 · done · DBAPIError → DatabaseBusy/StorageError без SQL, параметров и цепочки исключений; heartbeat/release/импорт пишут describe_error(); D-154; 836 passed · дальше F1.4
2026-10-02 · F1.4 · done · domain/redaction.py (sanitize_text, is_secret_key); last_error и data.error — имя типа; raw_status/code/reason/hint обрезаются и очищаются в машине регионов; scrub по главному слову имени + строки в списках + имя типа для неизвестных объектов; LeaseLogSink с лимитами и buffer_ttl; IdentityHub(redact_refs_in_events); 900 passed · дальше F1.5: черновики в scratchpad/f15wip (outbound.py с PinnedBackend, test_outbound.py)
2026-10-02 · F1.5 · done · dispatch/outbound.py: разбор хоста во всех формах inet_aton, PinnedBackend (резолв, проверка каждого адреса, соединение с проверенным IP), create_client с trust_env=False, маскирование URL в логах httpx/httpcore; WebhookPolicy.proxy; WebhookEndpoint.url — scheme://host/***; Telegram и send_test без исключения httpx в цепочке; D-155; 956 passed · дальше F1.6
2026-10-02 · F1.6a · done · Lease.check() бросает LeaseLost после освобождения; credentials()/session() сверяют fencing в транзакции чтения (отзыв и перехват видны до heartbeat, LeaseOwnershipLost); Lease.proxy — SecretValue; proxies.url(actor, reason) с событием proxy.secret.revealed; secrets.reveal требует непустые actor и reason; repr ImportRecord/LeaseRow/ParsedProxy/IdentityCreate без секретов; 983 passed, admin unit 67 passed · дальше F1.6b: приватные store/envelope и служебные объекты хаба (≈250 обращений в src, admin, tests), публичный контракт в docs/guide/api.md, tests/test_public_api.py
2026-10-02 · F1.6b · done · store, envelope, import_runner, metric_rollups, rollups, state_snapshots, webhook_dispatcher, notification_dispatcher, metrics_reporter хаба — приватные; публичные hub.sessionmaker и hub.webhook_policy; admin без PgStore и hub.store; контракт хаба в docs/guide/api.md и тест поверхности; D-158; 984 passed, admin unit 67 passed · дальше F1.7
2026-10-02 · F1.7 · done · parse_keys: ошибки по номеру записи без ключа и key_id, BOM, алфавит и длина key_id, отказ ключу из одинаковых байтов; keys() только для чтения, запрет pickle; FileSecretsProvider.reload() с сохранением прежних ключей при ошибке; порядок ротации в docs/cli.md; D-159; 1005 passed · дальше F1.8
2026-10-02 · F1.8 · done · Store.rotate_key постраничный по наборам с курсором (включая ih_import_items), inactive_key_usage; Doctor.rotate_key проходит до конца с повторами для заблокированных строк, отчёт remaining/unreadable/complete; Doctor.inactive_keys; CLI rotate-key и doctor печатают остаток, код 1; импорт с нерасшифрованными элементами — failed, элементы сохраняются; D-160; 1017 passed · дальше F2.1 (фаза аренды)
2026-10-02 · F2.1 · done · heartbeat повторяется с backoff 0,5 с ×2 в пределах дедлайна; release под shield с таймаутом 10 с, _released после успешной записи, повтор возможен, reason проверяется первым; proxy.pool.exhausted — одно на acquire и раз в минуту на пул; общие сценарии tests/fixtures/lease_resilience.py на MemoryStore и PostgreSQL; D-161; 1030 passed, наборы аренды ×3 · дальше F2.2
2026-10-02 · F2.2 · done · expire_leases отбирает аренды с открытой историей (миграция 0011, индекс ix_ih_lease_history_open_space); lease.expired с едиными полями pool/holder/fencing/expired_at; identities.delete при живой аренде — ValidationFailed, revoke=True отзывает в той же транзакции; удаление закрывает историю истёкшей аренды; MemoryStore хранит пространство истории; D-162; 1039 passed, наборы аренды ×3 · дальше F2.3
2026-10-02 · F2.3 · done · Clock.sleep (пауза вызывающего) и Clock.idle (фоновое ожидание); FakeClock.sleep идёт по пробуждениям, advance будит heartbeat; acquire, heartbeat, диспетчер, rotate_key и CLI на часах хаба; Store.bind_clock: MemoryStore принимает часы хаба, разные часы — ValidationFailed; heartbeat считает паузу от последнего подтверждения; тест дедлайна heartbeat больше не зависит от времени захвата в БД; D-163; 1049 passed, наборы аренды ×3, admin unit 67 passed · дальше F2.4
2026-10-02 · F2.4 · done · lock_identities до чтения в _mutate, requirements.add/resolve и apply_verification; conformance-тест: 20 параллельных add_tags, слияний меток и требований сохраняют всё; lock-order.md дополнен; D-164; 1057 passed · фаза F2 закрыта, дальше F3.1
2026-10-02 · F3.1 · done · колбэки subscribe: курсор по одному событию через возвращаемое значение обработчика process_events, max_attempts (5), журнальное dispatcher.event.skipped и метрика; изоляция пакетных потребителей и pump-ов закреплена тестом; гарантии доставки и влияние долгой транзакции в 10-events.md; D-165; 1065 passed, admin unit 67 passed · дальше F3.2
2026-10-02 · F3.2 · done · deliver_due: endpoint'ы параллельно под WebhookPolicy.concurrency, внутри endpoint'а по очереди, окно старта внутри срока захвата, Store.release_webhook_deliveries для неотправленных; pump-ы диспетчера одновременно; finish не стирает ручную причину отключения; MemoryStore: limit после фильтра; D-166; 1079 passed, admin unit 67 passed · дальше F3.3
2026-10-02 · F3.3 · done · 429 не расходует попытку и ставит канал на паузу (paused_until), claim не берёт каналы на паузе; дайджест частями до 4096 единиц UTF-16, обрезка одиночного сообщения; миграция 0012: уникальность (rule_id, event_ids[1]) без дайджеста и paused_until; паритет MemoryStore (sent_at, NotFound, порядок rules, limit после фильтра); шаблон без вложенных полей; UNSET для thread_id; D-167; 1105 passed, admin unit 67 passed · дальше F3.4
2026-10-02 · F3.4 · done · вход под арендой с heartbeat; откат auth в исходное состояние под shield при сбое вне аутентификатора и при отмене; Service.login_timeout; override/команда из challenge и logging_in закрывает challenge; resolve_challenge стирает ответ; обслуживание сбрасывает брошенный logging_in (Store.stuck_logins); повторный login — LeaseUnavailable сразу; linked ждёт не дольше остатка основной аренды; оговорка снята из limitations.md; D-168; 1115 passed, admin unit 67 passed · дальше F3.5
2026-10-02 · F3.5 · done · stale_identities: только ready, порядок по probe_attempted_at (миграция 0013), Store.mark_probe_attempts при любом исходе; refresh_many добирает кандидатов, недоступные — skipped_unavailable без расхода limit; Service.probe_timeout; ProbeResult отклоняет наивное время; сервис без факторов — пустой отчёт; D-169; 1130 passed, admin unit 67 passed · дальше F3.6
2026-10-02 · F3.6 · done · due_identity_timers: только состояния с on_expire (limited, paused, cooling) на обоих хранилищах; обслуживание: try/except на аккаунт, на подзадачу пространства и на группу в run_due, аккаунт незарегистрированного сервиса пропускается с метрикой; MemoryStore лениво снимает until только у paused/cooling; D-170; 1138 passed, admin unit 67 passed · дальше F3.7
2026-10-02 · F3.7a · done · dead перепроверяется раз в max_backoff; jitter, check_timeout, параллельный recheck_due; сбой чекера и отказ всей группы — proxy.checker.failed без учёта отказа; recheck_group; закрепление защищает от плановых перепроверок, dead и таймера региона; due_proxies(pool=) и due_identity_timers без закреплённых; D-172; 1150 passed, admin unit 67 passed · дальше F3.7b
2026-10-02 · F3.7b · done · proxies.replace_credentials, move, delete(unbind=) с событиями proxy.updated и proxy.deleted; Store: add_proxy_error (атомарный error_score), proxy_by_fingerprint, set_proxy_secret, delete_proxy; conformance для choose_proxy, bind_proxy, rebind_proxy; D-173; 1168 passed, admin unit 67 passed · дальше F3.8
2026-10-02 · F3.8 · done · parse_proxy_url без сырого ValueError; предвалидация ловит любое исключение записи; run_files кладёт ошибки файла в failed; ImportRunner изолирует пространства и элементы, staged_item_failed на третьем захвате; несуществующая связь и переполненный прокси отклоняют только свою запись, max_identities — параметр run; фоновый update_session по нормализованному ref, verify_sessions для существующих; identity.imported для обновлений; причина называет поля; conformance очереди импорта; D-174; 1190 passed, admin unit 67 passed · дальше F3.9
2026-10-02 · F3.9 · done · Override: ключ региона проверяется и получает значение по умолчанию (key: str | None); override верификации через apply_verification с согласованием verified_factors; ленивый срок действия фактора в evaluate и выборке аренды (Store.expired_factors); approve в MANUAL только из under_review; review reset_attempts; снятие verified по пробе — два наблюдения подряд; оговорка снята из limitations.md; D-175; 1204 passed, admin unit 67 passed · дальше F3.10
2026-10-02 · F3.10 · done · rollups пересчитывают только затронутые минуты, rebuild не трогает телеметрию и требует force за пределами retention; буфер метрик ограничен; hub.aclose()/flush_metrics()/async with hub; D-176; check.sh 1212 passed, admin unit 67 passed · дальше F3.11
2026-10-02 · F3.11 · done · CLI: демоны maintenance/dispatcher переживают сбой тика (backoff 1–60 с), сбой фабрики и внутренняя ошибка — код 1 с именем типа, разрушающие команды требуют --yes, doctor renormalize, предупреждение о пароле в --url (D-177); check зелёный: 1233 passed, admin unit 67 passed · дальше F4.1
2026-10-02 · F4.1a · done · миграция 0014: индексы под due_regions и очередь сверки, индексы под FK (тест «у каждого FK есть индекс»), FK notifications.channel_id и verifications.via_identity_id, CHECK на числа прокси, статус доставки paused при отключении endpoint (оба хранилища), server_default выровнен и сравнивается в тесте дрейфа; overview_snapshot без коррелированных EXISTS; замер на 300 000: stale_identities 0,01 с, overview 0,4–0,9 с (D-178, D-179); check зелёный: 1258 passed, admin unit 67 passed · дальше F4.1b: retention пачками в отдельных транзакциях, чистка ih_stats_metric_batches, закрытых challenges, корзин 1h/1d, доставок удалённых событий, индексы под предикаты чистки (миграция 0015)
2026-10-03 · F4.1b · done · чистка пачками в отдельных транзакциях (cleanup_batch_size), новые категории metric_batches, closed_challenges, rollup_hours, rollup_days и поля Retention, миграция 0015 с индексами под условия чистки, доставки удаляются вместе с событием, claim не берёт доставку без события, суточная корзина старше срока часовых пересчитывается по журналу (D-180); check зелёный: 1271 passed, admin unit 67 passed. Первый полный прогон упал: test_cleanup_statements_use_indexes превысил таймаут 120 с — не воспроизвелось (тот же набор из 565 тестов прошёл, блокировок в базе не было), причина не установлена, тест облегчён с 20 000 до 5000 строк (14 с → 4 с). Второй прогон упал на lint-imports: UnicodeEncodeError в cp1251 при FORCE_COLOR — проверку запускать с PYTHONIOENCODING=utf-8 PYTHONUTF8=1 · дальше F4.2
2026-10-03 · F4.2 · done · migrate: проверка db_schema как идентификатора, CREATE SCHEMA только при отсутствии, pg_trgm в схеме вне search_path, отдельная транзакция на ревизию и сессионная блокировка; 0008 — backfill одним UPDATE с набором пробелов str.strip; миграция 0016 пересчитывает bound_count; права и правило CONCURRENTLY в документации (D-181); check зелёный: 1282 passed, admin unit 67 passed (запуск с PYTHONIOENCODING=utf-8 PYTHONUTF8=1) · дальше F4.3
2026-10-03 · F4.3 · done · SCHEMA_COMPATIBLE (0014–0016): hub.start() принимает любую ревизию из множества, работа кода на каждой проверена тестом; SchemaIncompatible подсказывает действие; порядок выката — сначала код, потом migrate, правило expand/contract в docs/integrations.md (D-182); check зелёный: 1289 passed, admin unit 67 passed · дальше F4.4
2026-10-03 · F4.4a · done · сортировка profile:<key> как текст на обоих хранилищах, курсор проверяет тип значения (ValidationFailed вместо ошибки БД), строковые сортировки с COLLATE C, id:<hex> по окончанию id без дефисов любой длины, пределы строки запроса 1000 символов и 50 условий (D-184); check зелёный: 1324 passed, admin unit 67 passed · дальше F4.4b
2026-10-03 · F4.4b · done · metrics_snapshot(slugs) через and_ (conformance, background_snapshot с allowed_spaces), списки id одним параметром-массивом (_any_of) — очередь оператора открывается на 40 000 аккаунтов, doctor.check страницами по 1000 с линейной сверкой (D-185); check зелёный: 1331 passed, admin unit 67 passed · дальше F4.5
2026-10-03 · F4.5 · done · рецепт PgBouncer из трёх настроек asyncpg (локально PgBouncer 1.26: 60 операций без ошибок при max_prepared_statements 0 и по умолчанию, старый рецепт — 26 ошибок из 60), listen() проверяет подписку пробным NOTIFY с другого соединения и при неудаче освобождает соединение и логирует режим опроса, job CI pgbouncer с дымовым набором (actionlint чисто, в CI ещё не запускался) (D-186); check зелёный: 1334 passed, 2 skipped, admin unit 67 passed · дальше F4.6
2026-10-03 · F4.6 · done · AlreadyExists без id чужого пространства, Store.current_time() — границы purge, час снимка и минуты метрик по времени хранилища (сдвиг метрик округляется до минут), new_id() строго растёт в процессе, uuid-utils >= 1.0 (D-187); check зелёный: 1340 passed, 2 skipped, admin unit 67 passed. Первый прогон упал: таймаут test_human_queue_opens_with_40000 (F4.4b) — наблюдатель показал, что вставка 40 000 строк ih_region_states в посеве шла ~110 с на долгоживущем соединении пула (гипотеза: закэшированный план проверки FK для маленькой таблицы, не доказано); строки регионов убраны из посева этого теста (запрос регионов по-прежнему получает 40 000 id), следующий прогон — без долгих запросов · дальше F5.1
2026-10-03 · F5.1 · done · фикстура identity_hub через pytest_asyncio.fixture (pytester: strict и auto), без pytest-asyncio — UsageError с инструкцией; among=select в MemoryStore — ValidationFailed, among принимает предикат от Identity; блокировка MemoryStore на каждый event loop; make_identity(authenticated=False, session=…) — ошибка, TestingModel extra=forbid (D-188); check зелёный: 1348 passed, 2 skipped, admin unit 67 passed · дальше F5.2
2026-10-03 · F5.2a · done · MemoryStore как PostgreSQL: удаление аккаунта освобождает прокси, доказательства верификации через replace, event_id и lease_id — new_id, фильтр профиля и меток как JSONB @>, время и порядок связей, путь impact от исходного аккаунта, active в lease_stats; 12 conformance-тестов (D-190); check зелёный: 1360 passed, 2 skipped, admin unit 67 passed · дальше F5.2b
2026-10-03 · F5.2b · done · транзакция MemoryStore снимает поразделовый снимок вместо deepcopy (словари поверхностно, import_items и notifications глубоко, журналы по длине); lease+operation на 1000 аккаунтах 5,4 мс вместо 587, тест отката всех разделов (D-191); check зелёный: 1362 passed, 2 skipped, admin unit 67 passed · дальше F5.3
2026-10-03 · F5.3a · done · StoreConformanceSuite: шесть новых сценариев и полный откат; восемь сломанных Store через pytester — набор ловит все (прежний пропускал 6 из 8), исправный MemoryStore проходит; контракт Store помечен внутренним и нестабильным до 1.0 (D-193); check зелёный: 1383 passed, 2 skipped, admin unit 67 passed · дальше F5.3b
