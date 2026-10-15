# Замена credentials не сбрасывает регион `auth`

**Библиотека:** `identity_hub==1.0.0a2`. Пути — от `site-packages/identity_hub/`. Найдено чтением кода и тестами server-admins (хаб на `MemoryStore` и на PostgreSQL).

**Тип:** поведение. **Важность:** средняя.

После `Fault(SESSION_INVALID, code="credentials_rejected")` код остаётся в строке региона `auth`
(`operation.py::_on_failure`, стр. 247–262). Замена credentials его не меняет: код держится до
следующего входа (проверено на PostgreSQL). Карточка показывает «credentials отклонены» и после
того, как оператор ввёл новые.

**Предложение:** при `identity.credentials.updated` переводить `auth` в «ожидает входа» и очищать
код отказа, либо описать в документации, что сброс — забота приложения.
