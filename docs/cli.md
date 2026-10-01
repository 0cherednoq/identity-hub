# Командная строка Identity Hub

Это справочник для разработчика или оператора, который запускает миграции и
фоновые процессы Identity Hub. Все команды выполняются через установленный
скрипт `identity-hub`; в uv-проекте добавляйте префикс `uv run`.

## Общие настройки

| Переменная | Назначение | Где используется |
|---|---|---|
| `IDENTITY_HUB_DATABASE_URL` | Async SQLAlchemy URL PostgreSQL | `migrate`, фабрика приложения |
| `IDENTITY_HUB_KEK` | `key_id:base64`; первый ключ активен | `EnvSecretsProvider` в фабрике |
| `IDENTITY_HUB_FACTORY` | `package.module:callable` | Все команды, кроме `migrate` |

Значения `IDENTITY_HUB_KEK` и пароли в URL не передавайте аргументами процесса и
не пишите в логи. Для Docker/Kubernetes secrets можно использовать
`FileSecretsProvider` внутри фабрики.

Фабрика без аргументов должна вернуть `IdentityHub` или awaitable с ним. CLI сам
вызывает `hub.start()`:

```python
# app/identity.py
from identity_hub.hub import IdentityHub


async def create_hub() -> IdentityHub:
    return IdentityHub(...)
```

```bash
export IDENTITY_HUB_FACTORY=app.identity:create_hub
```

## `migrate`

Применяет внутреннюю ветку миграций до `head`:

```bash
identity-hub migrate \
  --url postgresql+asyncpg://app:password@db/app \
  --db-schema identity_hub
```

`--url` можно заменить переменной `IDENTITY_HUB_DATABASE_URL`. `--db-schema`
создаёт отдельную схему, если её ещё нет. `--revision` полезен только для
контролируемого развёртывания конкретной ревизии.

Миграция берёт PostgreSQL advisory lock на схему на всё время upgrade: параллельные
`migrate` (несколько подов с init-job) выполняются по очереди, второй видит уже готовую
схему. Блокировка транзакционная и снимается при commit или ошибке. Обычная работа
приложения этот lock не берёт.

## `dispatcher`

Доставляет Python-события, вебхуки и уведомления, а также запускает due-задачи:

```bash
identity-hub dispatcher --factory app.identity:create_hub
identity-hub dispatcher --once
```

Без `--once` процесс работает до отмены. Доставка at-least-once: пользовательские
callback и принимающие системы должны дедуплицировать побочные эффекты.

## `maintenance`

Нормализует истёкшие состояния, закрывает аренды и удаляет данные старше retention:

```bash
identity-hub maintenance
identity-hub maintenance --once
```

Постоянный процесс проверяет расписание раз в секунду. Не запускайте его вместо
`dispatcher`: у процессов разные циклы, хотя dispatcher также вызывает due-задачи.
Допустимо держать несколько экземпляров — операции хранилища конкурентно безопасны.

## `doctor`

Проверяет денормализованные регионы, факторы верификации, последовательности
событий и счётчики привязок прокси:

```bash
identity-hub doctor
identity-hub doctor --fix
```

Без `--fix` наличие расхождений даёт код возврата 1. С `--fix` найденные
расхождения исправляются и команда возвращает 0; изменения всё равно следует
проверить по журналу и метрикам.

## `rotate-key`

После добавления нового KEK первым в конфигурации переоборачивает зашифрованные
DEK пакетами:

```bash
export IDENTITY_HUB_KEK='k2:<новый-base64>,k1:<старый-base64>'
identity-hub rotate-key --batch-size 500
```

Команда повторяет пакеты до завершения и печатает число изменённых записей.
Старый ключ удаляйте из конфигурации только после успешной ротации всех
экземпляров приложения и резервных копий, которые должны оставаться читаемыми.

## Диагностика CLI

```bash
identity-hub --version
identity-hub --help
identity-hub dispatcher --help
```

Ошибки конфигурации возвращают код 2 через argparse. Ошибки библиотеки печатаются как
`error[<code>]: <сообщение>` и возвращают код выхода по коду ошибки:

| Код ошибки | Exit |
|---|---|
| `validation_error`, `not_found` | 2 |
| `schema_incompatible` — ревизия схемы не та, что ожидает код | 3 |
| `database_busy` | 4 |
| `secret_configuration_error`, `decryption_failed` | 5 |
| прочие | 1 |

Бесконечные процессы должны запускаться под supervisor с корректной доставкой
SIGTERM/отмены.
