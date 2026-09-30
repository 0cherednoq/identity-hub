# Identity Hub Admin

Отдельный web-пакет для управления Identity Hub. Python backend использует только публичные
контракты `identity_hub`, а React SPA собирается в статику Python-пакета.

## Требования

- Python 3.12–3.14;
- uv;
- Node.js 22 или 24 и npm 10+.

## Первый запуск

```powershell
uv sync --project admin --all-groups
npm --prefix admin/frontend ci
uv run --project admin poe -C admin check-backend
uv run --project admin poe -C admin check-frontend
uv run --project admin poe -C admin dev
```

Backend слушает `http://localhost:8765`, Vite — `http://localhost:5173` и проксирует `/api` и
`/health` на backend. Команда `dev` запускает безопасный in-memory demo: четыре пространства для
loading, empty, error и large dataset состояний, 258 синтетических аккаунтов и ни одного реального
credential. `Ctrl+C` завершает обе группы процессов.

Те же шаги в Unix shell:

```bash
uv sync --project admin --all-groups
npm --prefix admin/frontend ci
uv run --project admin poe -C admin check-backend
uv run --project admin poe -C admin check-frontend
uv run --project admin poe -C admin dev
```

После появления сообщений о готовности откройте `http://localhost:5173`. Повторный seed в том же
процессе идемпотентен: библиотека находит аккаунты по `(service, external_ref)` и не создаёт дубликаты.
Если локальные порты заняты, задайте `IH_ADMIN_DEV_BACKEND_PORT` и `IH_ADMIN_DEV_FRONTEND_PORT` перед
командой `dev`; Vite proxy будет настроен на выбранный backend автоматически.

Живой каталог дизайн-системы доступен на `http://localhost:5173/_design`. Он содержит обе темы,
все доменные статусы, loading/empty/error-состояния, граничные числа и длинные строки.

## Команды

| Команда | Назначение |
|---|---|
| `uv run --project admin poe -C admin dev` | Backend с reload и Vite HMR параллельно |
| `uv run --project admin identity-hub-admin demo` | Только seeded demo backend без Vite |
| `uv run --project admin identity-hub-admin hash-password` | Интерактивно создать Argon2 hash без пароля в shell history |
| `uv run --project admin poe -C admin check-backend` | Ruff, форматирование, mypy и pytest |
| `uv run --project admin poe -C admin check-frontend` | TypeScript, ESLint, Prettier и Vitest |
| `uv run --project admin poe -C admin test-e2e` | Chromium E2E через Playwright |
| `uv run --project admin poe -C admin test-visual` | Сравнить `/_design` и shell в light/dark desktop/mobile с эталонными снимками |
| `uv run --project admin poe -C admin build` | Собрать SPA и wheel |
| `uv run --project admin poe -C admin generate-client` | Обновить OpenAPI snapshot и generated TypeScript client |
| `uv run --project admin poe -C admin check-client` | Проверить drift OpenAPI и generated client без ручного сравнения |

Перед первым локальным E2E-запуском установите Chromium:

```powershell
npm run test:e2e:install --prefix admin/frontend
uv run --project admin poe -C admin test-e2e
```

Playwright сохраняет screenshot, video и trace при падении в `admin/test-results/`. Backend- и
integration-тесты используют `IH_TEST_DATABASE_URL`, а без него автоматически запускают
`postgres:16-alpine` через testcontainers.

E2E запускает изолированный `identity_hub_admin.testing:app`: три детерминированные роли, безопасные
demo-пространства и harmless operator probe не подключаются ни к development-, ни к production-
entry point. Набор проверяет реальный password login/session/logout, backend RBAC, axe и управление
только клавиатурой. Правило обновления visual baselines и команда описаны в
[`frontend/tests/e2e/README.md`](frontend/tests/e2e/README.md); новый снимок принимается только вместе
с объяснением намеренного изменения интерфейса.

## Конфигурация standalone

`identity-hub-admin serve` читает переменные с префиксом `IH_ADMIN_`.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `IH_ADMIN_ENVIRONMENT` | `development` | `development`, `test` или `production` |
| `IH_ADMIN_DATABASE_URL` | — | SQLAlchemy async URL PostgreSQL |
| `IH_ADMIN_HUB_FACTORY` | — | Фабрика `IdentityHub` в формате `package.module:callable` |
| `IH_ADMIN_KEK_KEYS` / `IH_ADMIN_KEK_ACTIVE_ID` | — | Secret-конфигурация ключей, обязательна в production |
| `IH_ADMIN_AUTH_MODE` | `token` | `token`, `password` или `trusted_proxy` |
| `IH_ADMIN_BEARER_TOKENS` | — | JSON `token → {actor, role, allowed_spaces}`; хранится как secret |
| `IH_ADMIN_PASSWORD_HASH` | — | Argon2 hash для единственного standalone пользователя |
| `IH_ADMIN_SESSION_SECRET` | — | Ключ подписи короткоживущей cookie-сессии |
| `IH_ADMIN_TRUSTED_PROXY_CIDRS` | пусто | CIDR доверенного proxy; forwarded-заголовки от других адресов отклоняются |
| `IH_ADMIN_EXTERNAL_TIMEOUT_SECONDS` | `15` | Таймаут startup-проверок и внешних зависимостей |
| `IH_ADMIN_SHUTDOWN_TIMEOUT_SECONDS` | `10` | Таймаут закрытия engine и подключённых ресурсов |
| `IH_ADMIN_HOST` / `IH_ADMIN_PORT` | `127.0.0.1` / `8765` | Адрес standalone-сервера |
| `IH_ADMIN_BASE_PATH` | пусто | URL-префикс при публикации под подпутём |
| `IH_ADMIN_STATIC_DIR` | packaged `static/` | Переопределение production SPA |
| `IH_ADMIN_DEMO_MODE` | `false` | In-memory demo; значение `true` запрещено в production |

Проверить конфигурацию и наличие production SPA без запуска сервера:

```powershell
uv run --project admin identity-hub-admin check
```

Demo mode не подключается к PostgreSQL, не читает `IH_ADMIN_KEK_KEYS`, не запускает dispatcher и не
отправляет сетевые запросы. Для standalone production используйте только `identity-hub-admin serve`;
startup отклонит сочетание `IH_ADMIN_ENVIRONMENT=production` и `IH_ADMIN_DEMO_MODE=true`.

`GET /health/diagnostics` возвращает readiness checks, request ID и агрегированные HTTP-метрики без
секретов. Structured access log содержит actor, route, space, status и latency, но никогда не
содержит query string или request body.

## Аутентификация

Bearer-конфигурация — JSON-объект, где ключ является самим токеном. Не сохраняйте значение в
репозитории или shell history; передавайте его через secret manager окружения:

```json
{
  "replace-with-random-token": {
    "actor": "service:operations",
    "role": "operator",
    "allowed_spaces": ["alpha", "beta"]
  }
}
```

Для password mode сначала выполните `identity-hub-admin hash-password`, затем задайте полученный
Argon2 hash в `IH_ADMIN_PASSWORD_HASH`, случайный ключ подписи в `IH_ADMIN_SESSION_SECRET` и
`IH_ADMIN_AUTH_MODE=password`. Сессия живёт один час по умолчанию. Cookie имеет `Secure`,
`HttpOnly` и `SameSite=Lax`; изменяющие cookie-запросы дополнительно требуют значение cookie
`ih_admin_csrf` в заголовке `X-CSRF-Token`. Bearer-запросам CSRF не нужен.

Trusted proxy mode принимает `X-Admin-Actor`, `X-Admin-Role` и `X-Admin-Spaces` только когда
`IH_ADMIN_AUTH_MODE=trusted_proxy` и непосредственный IP клиента входит в
`IH_ADMIN_TRUSTED_PROXY_CIDRS`. Заголовки от любого другого адреса отклоняются, даже если выглядят
корректно. Reverse proxy должен удалять эти заголовки из входящего пользовательского запроса и
устанавливать собственные после OIDC-аутентификации.

Архитектурные контракты и порядок автономной реализации находятся в
[`docs/admin`](../docs/admin/README.md).
