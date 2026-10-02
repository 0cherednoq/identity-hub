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
| F2.3 | Часы и ожидание | F2.1 | todo |  |  |
| F2.4 | Потерянные обновления identity | F0.1 | todo |  |  |
| F3.1 | Диспетчер: изоляция сбойного события | F0.1 | todo |  |  |
| F3.2 | Вебхуки: параллельная доставка | F0.1 | todo |  |  |
| F3.3 | Уведомления Telegram | F0.1 | todo |  |  |
| F3.4 | Вход и challenge | F0.1 | todo |  |  |
| F3.5 | Сверка с платформой | F0.1 | todo |  |  |
| F3.6 | Обслуживание и таймеры | F0.1 | todo |  |  |
| F3.7 | Прокси | F0.1 | todo |  |  |
| F3.8 | Импорт | F0.1 | todo |  |  |
| F3.9 | Override и верификации | F0.1 | todo |  |  |
| F3.10 | Rollups, метрики и закрытие хаба | F0.1 | todo |  |  |
| F3.11 | CLI | F0.1 | todo |  |  |
| F4.1 | Retention, индексы и целостность | F0.1 | todo |  |  |
| F4.2 | Миграции: права и данные | F0.1 | todo |  |  |
| F4.3 | Совместимость ревизии схемы | F0.1 | todo |  |  |
| F4.4 | Админ-чтение на объёме и поиск | F0.1 | todo |  |  |
| F4.5 | PgBouncer | F0.1 | todo |  |  |
| F4.6 | Мелкие расхождения хранилища | F0.1 | todo |  |  |
| F5.1 | pytest-плагин и фабрики | F0.1 | todo |  |  |
| F5.2 | Паритет `MemoryStore` | F0.1 | todo |  |  |
| F5.3 | Conformance и раздел о своём Store | F2.2, F3.2, F3.3, F3.5, F3.6, F3.7, F3.8, F5.2 | todo |  |  |
| F6.1 | Пространства | F0.1 | todo |  |  |
| F6.2 | Единообразие API | F0.1 | todo |  |  |
| F6.3 | Адаптеры | F0.1 | todo |  |  |
| F6.4 | Удаление устаревших алиасов | F1.6b, F6.2, F6.3 | todo |  |  |
| F6.5 | Версия, язык и публичный контракт | F6.4 | todo |  |  |
| F7.1 | Финальная сверка | все задачи F0–F6 | todo |  |  |

**Итого:** 13 / 41 done

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
