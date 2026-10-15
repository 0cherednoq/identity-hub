# Причина неудачного входа не сохраняется

**Библиотека:** `identity_hub==1.0.0a2`. Пути — от `site-packages/identity_hub/`. Найдено чтением кода и тестами server-admins (хаб на `MemoryStore` и на PostgreSQL).

**Тип:** недостающий функционал. **Важность:** высокая.

При исключении из `Authenticator.login` (`auth.py::_login`, стр. 402–415) регион `auth` получает
`Command(NO_SESSION)` без кода (стр. 548), событие `identity.login.failed` несёт только
`data={"error": type(exc).__name__}`, операция `login` в `admin.provider_operations` —
`error_code=safe_error_code(exc)`, то есть тоже имя класса (`domain/provider_operations.py`,
стр. 81–83). Код и текст ответа платформы не сохраняются нигде, в карточке аккаунта причины нет.

Из-за этого приложение отличает «пароль отклонён» (нужны новые credentials, повтор бесполезен)
от сетевого сбоя (повтор поможет) только по имени класса исключения.

**Обход в server-admins:** адаптер переводит отказ в отдельный класс `CredentialsRejected`, а
признак «ждём новые credentials» хранит в своей таблице (`provider_accounts.credentials_rejected_at`).

**Предложение:** протокол ошибки входа с кодом (например, `LoginFailed(code, retryable)`); код —
в строке региона `auth` и в событии `identity.login.failed` (`data["code"]`).
