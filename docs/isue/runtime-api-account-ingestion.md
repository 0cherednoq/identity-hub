# Runtime Service Definitions

Статус: proposal

Приоритет: P1

Проверено на: `identity-hub==1.0.0a8`

Основной читатель: maintainer `identity-hub`
Тип документа: implementation issue

## Требуемый результат

Приложение может создать новый экземпляр сервиса после `IdentityHub.start()`, сразу импортировать его identity и использовать во всех уже работающих процессах без рестарта.

Runtime-сервис создаётся на основе Python-шаблона, установленного при deploy:

```python
definition, created = await hub.service_definitions.ensure(
    RuntimeServiceCreate(
        name="supplier_a_site_421",
        title="Example Shop",
        url="https://example.com",
        template="browser_account_checker/v1",
        config={},
    ),
    actor="supplier-sync",
    idempotency_key="supplier-a:site:421",
)

report = await space.imports.run(
    service=definition.name,
    records=records,
    on_existing="merge",
    actor="ingest:supplier-a",
)
```

`identity-hub` хранит definition, обеспечивает FK, revision, аудит и разрешение definition во всех процессах. Смысл `config` определяет зарегистрированный template. Библиотека не знает browser flow, CSS selectors или API поставщика.

## Почему текущий API недостаточен

`Service` является Python-объектом. Он содержит схемы, authenticator, probes, error classifier, политики операций, нормализаторы и функции.

`IdentityHub.register_service()` добавляет объект только в `_services` текущего экземпляра. `IdentityHub.start()` синхронизирует текущий набор в `ih_services`. Вызов `register_service()` после старта не является согласованной runtime-регистрацией:

- PostgreSQL требует родительскую строку `ih_services` для `ih_identities.service`;
- `MemoryStore` может принять сценарий, который не проходит на PostgreSQL;
- другие процессы не получают объект и его схемы;
- после рестарта объект исчезает;
- два процесса могут использовать разные definitions одного имени.

Текущий импорт уже подходит для runtime-аккаунтов. `Imports.run()` принимает `Iterable[ImportRecord] | AsyncIterable[ImportRecord]`, выполняет пакетные commit и возвращает `ImportReport`.

Исходный код:

- [`IdentityHub.register_service`](../../src/identity_hub/hub.py) изменяет `_services` экземпляра;
- [`IdentityHub.start`](../../src/identity_hub/hub.py) вызывает `sync_services(...)`;
- [`PgStore.sync_services`](../../src/identity_hub/postgres/store.py) заполняет `ih_services`;
- [`Imports.run`](../../src/identity_hub/api/imports.py) принимает sync/async поток;
- [`ImportingMixin._import_many`](../../src/identity_hub/importing.py) использует синхронный `hub.service(...)`.

## Граница ответственности

`identity-hub` отвечает за:

- публичный протокол template;
- долговечную runtime definition;
- CRUD и idempotency;
- optimistic revision;
- PostgreSQL/MemoryStore parity;
- async resolution и межпроцессную согласованность;
- enable/disable;
- аудит и redaction;
- совместимость static services.

Потребитель отвечает за:

- Pydantic-схему `config`;
- materialization `RuntimeServiceDefinition -> Service`;
- authenticator, probes и error classifier;
- проверку прикладной безопасности config;
- supplier adapter и scheduler;
- прикладной UI.

`config` является JSON-объектом, непрозрачным для Core. Core проверяет размер и JSON-совместимость, а template выполняет предметную валидацию до commit create/update.

## Публичные модели

```python
class ServiceTemplate(Protocol):
    name: str
    config_schema: type[BaseModel]

    def build(self, definition: RuntimeServiceDefinition) -> Service: ...


@dataclass(frozen=True, slots=True)
class RuntimeServiceCreate:
    name: str
    title: str
    url: str | None
    template: str
    config: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeServiceDefinition:
    name: str
    title: str
    url: str | None
    template: str
    config: Mapping[str, JsonValue]
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime
```

Ограничения:

- `name` использует действующие правила имени `Service`;
- `template` ссылается только на template, зарегистрированный в процессе;
- `config` не содержит callable, import path или исполняемый код;
- `config` проходит `template.config_schema` и `template.build()` до записи;
- результат `build()` обязан иметь то же `name`, `title` и `url`;
- template name уникален в процессе и фиксируется после `start()`.

## Регистрация templates

```python
hub = IdentityHub(
    ...,
    services=[STATIC_SERVICE],
    service_templates=[BrowserAccountCheckerTemplate(browser_pool)],
)
await hub.start()
```

Допустим явный pre-start API:

```python
hub.register_service_template(template)
await hub.start()
```

Новый template с новым Python-кодом требует deploy и restart. Новые definitions уже установленного template создаются без restart.

## CRUD namespace

Существующий `hub.services` остаётся tuple materialized `Service`. Новый API размещается отдельно:

```python
definition = await hub.service_definitions.create(spec, actor="admin")

definition, created = await hub.service_definitions.ensure(
    spec,
    actor="supplier-sync",
    idempotency_key="supplier-a:site:421",
)

definition = await hub.service_definitions.get("supplier_a_site_421")
page = await hub.service_definitions.list(template="browser_account_checker/v1", enabled=True)

updated = await hub.service_definitions.update(
    definition.name,
    title="Example Shop EU",
    config={...},
    expected_revision=definition.revision,
    actor="admin",
)

await hub.service_definitions.disable(
    definition.name,
    expected_revision=updated.revision,
    actor="admin",
    reason="supplier removed site",
)

await hub.service_definitions.enable(
    definition.name,
    expected_revision=updated.revision + 1,
    actor="admin",
)
```

### `create`

1. Проверяет имя и наличие template.
2. Валидирует config через `config_schema`.
3. Вызывает `template.build()` до транзакции записи.
4. В одной транзакции создаёт FK-родителя и runtime definition.
5. Пишет audit event.
6. Materializes revision в текущем процессе.

Существующее имя даёт `AlreadyExists`. Имя static service нельзя занять runtime definition.

### `ensure`

Рекомендуется для повторяемого supplier sync.

- одинаковый idempotency key и payload возвращают прежний результат;
- другой payload с тем же ключом даёт `IdempotencyConflict`;
- существующее имя с другим definition не обновляется скрыто;
- update выполняется отдельной явной командой.

### `update`

- требует `expected_revision`;
- валидирует и строит новую revision до commit;
- увеличивает revision только при реальном изменении;
- не активирует невалидную revision;
- не меняет origin static/runtime.

### `disable` и `enable`

Disabled definition:

- остаётся FK-родителем существующих identity;
- остаётся доступной для admin/read/audit;
- запрещает новые imports, create identity, login, lease, acceptance и probes;
- не удаляет credentials, sessions и историю.

Удаление runtime definition в первой версии отсутствует.

## Хранение

Предпочтительно расширить `ih_services`, поскольку таблица уже является FK-родителем identity.

| Поле | Тип | Назначение |
| --- | --- | --- |
| `name` | существующий PK | стабильное имя сервиса |
| `title`, `url` | существующие | отображение |
| `origin` | `static | runtime` | источник definition |
| `template` | nullable text | template runtime-сервиса |
| `config` | nullable JSONB | валидированная конфигурация |
| `enabled` | boolean | допуск новых операций |
| `revision` | bigint | optimistic concurrency и cache invalidation |
| `created_at`, `updated_at` | timestamptz | аудит |

Миграция помечает существующие строки `origin="static"`. Startup sync обновляет только static rows. Runtime rows не удаляются и не перезаписываются стартом приложения.

Если прикладной template допускает секретные поля, он обязан использовать отдельное secret storage. `ih_services.config` не является обходом `SecretsProvider`.

## Async resolver

```python
service = await hub.resolve_service("supplier_a_site_421")
```

Resolver:

1. возвращает static service без I/O;
2. при runtime cache miss читает definition из store;
3. сверяет `enabled` и revision;
4. находит template текущего процесса;
5. валидирует config и вызывает `build()`;
6. кэширует `(name, revision, Service)`;
7. перестраивает объект при изменении revision.

Событие `service.definition.changed` может ускорять invalidation, но корректность не зависит от доставки события. Async operation сверяет revision на границе работы.

Существующий `hub.service(name)` остаётся синхронным cache-only API. Он не выполняет скрытый I/O и работает для static либо уже materialized runtime service.

## Entry points, которые должны использовать resolver

- `imports.run`, `imports.run_files`, `imports.stage`;
- `identities.create`, `ensure`, `create_many`;
- создание/получение pool по имени сервиса;
- auth login, revalidation и session adoption;
- probes и sync;
- acceptance worker;
- maintenance, timers и background workers, обрабатывающие identity.

Операция, начатая по identity id, сначала загружает строку identity и затем async-resolves её service. Неизвестный или недоступный template не превращается в generic storage error.

## Ошибки

| Ситуация | Ошибка |
| --- | --- |
| Template не зарегистрирован в текущем процессе | `ServiceTemplateUnavailable` |
| Config не проходит schema/build | `ValidationFailed`, запись не меняется |
| Имя уже занято | `AlreadyExists` |
| Definition disabled | `OperationNotAllowed` |
| `expected_revision` устарела | `RevisionConflict` |
| Idempotency key повторён с другим payload | `IdempotencyConflict` |
| Runtime definition отсутствует | `NotFound("service", name)` |

`ServiceTemplateUnavailable` должен быть операционной ошибкой конфигурации процесса, а не доказательством проблемы identity.

## Аудит и безопасность

Мутации требуют `actor` и создают события:

- `service.definition.created`;
- `service.definition.updated`;
- `service.definition.enabled`;
- `service.definition.disabled`.

События содержат name, template, revision и изменённые безопасные поля. Полный config в event не попадает. Template либо объявляет sensitive paths, либо по контракту гарантирует отсутствие секретов.

Ограничения Core:

- максимальный размер config;
- JSON object на верхнем уровне;
- запрет NaN/Infinity и не-JSON типов;
- лимит definitions на space/tenant, если multi-tenant policy требует;
- нормализация и fingerprint idempotency без раскрытия config.

## Совместимость

- static services продолжают работать без изменения кода;
- `hub.services` и `hub.service()` сохраняются;
- startup sync не затрагивает runtime rows;
- новая схема БД обратно совместима с чтением static rows;
- downgrade не может удалить runtime columns/rows, пока существуют runtime identity;
- alpha-релиз должен описать API и migration в upgrade guide.

## План реализации

- [ ] **IH-1. Models.** Добавить public models и `ServiceTemplate` protocol.
- [ ] **IH-2. Migration.** Расширить `ih_services`, backfill static origin/revision/timestamps.
- [ ] **IH-3. Store contract.** CRUD definitions, idempotency и optimistic update для MemoryStore/PgStore.
- [ ] **IH-4. Namespace.** Реализовать `hub.service_definitions`.
- [ ] **IH-5. Template registry.** Pre-start registration, duplicate validation и freeze after start.
- [ ] **IH-6. Resolver.** Async resolve и revision-aware cache.
- [ ] **IH-7. Entry points.** Перевести service-by-name и identity-driven операции на resolver.
- [ ] **IH-8. Disabled semantics.** Guards для imports/auth/pools/workers.
- [ ] **IH-9. Events/redaction.** Аудит без утечки config.
- [ ] **IH-10. Conformance.** Memory/PG parity и multi-hub tests.
- [ ] **IH-11. Docs.** API guide, service guide, limitations и upgrade.
- [ ] **IH-12. Release.** Changelog, version bump и опубликованный release identifier для потребителей.

## Тестовая матрица

| Сценарий | Ожидаемый результат |
| --- | --- |
| create runtime definition | FK-родитель и revision 1 созданы атомарно |
| invalid config/build | строка не создаётся |
| ensure replay | один definition, прежний result |
| idempotency conflict | `IdempotencyConflict` |
| concurrent update | один commit, второй `RevisionConflict` |
| hub A create, hub B import | B materializes без restart |
| revision update | оба хаба перестраивают cache |
| disable | история читается, новые операции запрещены |
| static startup sync | runtime rows не изменяются |
| unknown template in worker | typed operational error, identity не портится |
| MemoryStore и PostgreSQL | одинаковый публичный результат |

## Критерии готовности релиза

- [ ] Runtime definition создаётся после `start()`.
- [ ] Уже работающий второй процесс использует её без restart.
- [ ] Аккаунты импортируются через существующий `imports.run()`.
- [ ] Runtime config обновляется с revision и cache invalidation.
- [ ] Disabled definition блокирует работу без удаления данных.
- [ ] Static services не требуют миграции пользовательского кода.
- [ ] MemoryStore и PostgreSQL проходят одинаковую conformance suite.
- [ ] Документированы ошибки, безопасность и upgrade.
- [ ] Выпущена версия `identity-hub`, которую может закрепить потребитель.

## Не входит в задачу

- browser automation;
- CSS selectors и login flow;
- классификация invalid password/ban/deleted;
- встроенный supplier HTTP client;
- supplier scheduler/cursor;
- UI управления сайтами;
- загрузка Python/JavaScript-кода из БД;
- сериализация authenticator/probes;
- удаление runtime definition;
- exactly-once между внешним API и БД.

## Первый потребитель и release gate

Первый потребитель: `checker_account`, template `browser_account_checker/v1`.

План потребителя: `checker_account/docs/RUNTIME_BROWSER_CHECKER_PLAN.md`.

Интеграционный этап потребителя начинается после релиза `identity-hub`, в котором выполнены критерии этого документа. Потребитель закрепляет точную выпущенную версию и не зависит от приватных методов или незарелизованной схемы.
