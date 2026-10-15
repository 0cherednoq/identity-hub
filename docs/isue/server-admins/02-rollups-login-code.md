# Сводка `logins.failed` всегда пустая

**Библиотека:** `identity_hub==1.0.0a2`. Пути — от `site-packages/identity_hub/`. Найдено чтением кода и тестами server-admins (хаб на `MemoryStore` и на PostgreSQL).

**Тип:** ошибка. **Важность:** средняя.

`rollups.py` (стр. 102–103) группирует неудачные входы по `data["code"]` события
`identity.login.failed`, а `auth.py::_login` пишет в событие только `data["error"]`
(стр. 403, 409). Ключа `code` нет никогда, поэтому измерение `logins.failed` в сводках пустое.

**Предложение:** писать `code` в событие (см. issue 01) или считать сводку по `error`.
